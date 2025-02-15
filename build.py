import argparse
import os
import pickle
from platform import system
from typing import List

import tvm
import tvm.testing
from tvm import relax

import mlc_llm
from mlc_llm import utils
from mlc_llm.relax_model import gpt_neox, llama, moss


def _parse_args():
    args = argparse.ArgumentParser()
    utils.argparse_add_common(args)
    args.add_argument("--quantization-sym", action="store_true", default=False)
    args.add_argument(
        "--quantization-mode", type=str, choices=["int4", "int3", "fp4"], default="int4"
    )
    args.add_argument(
        "--quantization-storage-nbit", type=int, choices=[32, 16], default=32
    )
    args.add_argument("--no-quantize", action="store_true", default=False)
    args.add_argument("--max-seq-len", type=int, default=-1)
    args.add_argument("--target", type=str, default="auto")
    args.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Path to log database. Default: ./log_db/{model}",
    )
    args.add_argument("--artifact-path", type=str, default="dist")
    args.add_argument(
        "--use-cache",
        type=int,
        default=1,
        help="Whether to use previously pickled IRModule and skip trace.",
    )
    args.add_argument("--debug-dump", action="store_true", default=False)
    args.add_argument("--debug-load-script", action="store_true", default=False)

    args.add_argument(
        "--llvm-mingw",
        type=str,
        default="",
        help="/path/to/llvm-mingw-root, use llvm-mingw to cross compile to windows",
    )
    args.add_argument("--system-lib", action="store_true", default=False)

    parsed = args.parse_args()
    assert parsed.max_seq_len == -1 or parsed.max_seq_len > 0

    parsed.model_path = os.path.join(parsed.artifact_path, "models", parsed.model)
    parsed.artifact_path = os.path.join(
        parsed.artifact_path, parsed.model, parsed.dtype
    )
    parsed.export_kwargs = {}
    parsed.lib_format = "so"
    parsed.db_path = parsed.db_path or os.path.join("log_db", parsed.model)
    utils.parse_target(parsed)
    utils.argparse_postproc_common(parsed)
    return parsed


def debug_dump_script(mod, name, args):
    """Debug dump mode"""
    if not args.debug_dump:
        return
    dump_path = os.path.join(args.artifact_path, "debug", name)
    with open(dump_path, "w") as outfile:
        outfile.write(mod.script(show_meta=True))
    print(f"Dump mod to {dump_path}")


def debug_load_script(name, args):
    input_path = os.path.join(args.artifact_path, "debug", name)
    lib = {"__file__": input_path}
    exec(compile(open(input_path, "rb").read(), input_path, "exec"), lib, lib)
    return lib["Module"]


def debug_dump_shader(ex, name, args):
    """Debug dump mode"""
    if not args.debug_dump:
        return
    target_kind = args.target.kind.default_keys[0]
    suffix_map = {
        "webgpu": ".wgsl",
        "cuda": ".cu",
        "metal": ".mtl",
    }
    suffix = suffix_map.get(target_kind, ".txt")
    dump_path = os.path.join(args.artifact_path, "debug", name + suffix)
    source = ex.mod.imported_modules[0].imported_modules[0].get_source()
    with open(dump_path, "w") as outfile:
        outfile.write(source)
    print(f"Dump shader to {dump_path}")


def mod_transform_before_build(
    mod: tvm.IRModule,
    model_params: List[tvm.nd.NDArray],
    args: argparse.Namespace,
) -> tvm.IRModule:
    """First-stage: Legalize ops and trace"""
    model_names = ["encoding", "decoding", "create_kv_cache"]

    if not args.no_quantize:
        mod = mlc_llm.transform.GroupQuantize(
            group_size=40 if args.quantization_mode.endswith("3") else 32,
            sym=args.quantization_sym,
            mode=args.quantization_mode,
            storage_nbit=args.quantization_storage_nbit,
            dtype=args.dtype,
        )(mod)
    mod = mlc_llm.transform.FuseTransposeMatmul()(mod)

    mod = relax.pipeline.get_pipeline()(mod)
    mod = mlc_llm.transform.FuseDecodeMatmulEwise(args.dtype)(mod)
    mod = relax.transform.DeadCodeElimination(model_names)(mod)
    mod = relax.transform.LiftTransformParams()(mod)
    mod_transform, mod_deploy = utils.split_transform_deploy_mod(mod, model_names)

    debug_dump_script(mod_transform, "mod_lift_params.py", args)

    new_params = utils.transform_params(mod_transform, model_params)
    utils.save_params(new_params, args.artifact_path)
    return mod_deploy


def build(mod_deploy: tvm.IRModule, args: argparse.Namespace) -> None:
    target_kind = args.target_kind
    debug_dump_script(mod_deploy, "mod_before_build.py", args)
    if target_kind != "cpu":
        from tvm import meta_schedule as ms

        if os.path.exists(args.db_path):
            db = ms.database.create(work_dir=args.db_path)
        else:
            db = ms.database.MemoryDatabase()
        with db, tvm.target.Target("apple/m1-gpu-restricted"):
            mod_deploy = relax.transform.MetaScheduleApplyDatabase()(mod_deploy)
            mod_deploy = mlc_llm.transform.DispatchTIROperator(args.model_category)(
                mod_deploy
            )
            mod_deploy = tvm.tir.transform.DefaultGPUSchedule()(mod_deploy)
            mod_deploy = tvm.tir.transform.ForceNarrowIndexToInt32()(mod_deploy)

    if args.debug_load_script:
        mod_deploy = debug_load_script("mod_build_stage_debug.py", args)

    debug_dump_script(mod_deploy, "mod_build_stage.py", args)

    ex = relax.build(mod_deploy, args.target, system_lib=args.system_lib)

    output_filename = f"{args.model}_{target_kind}_{args.dtype}.{args.lib_format}"

    debug_dump_shader(ex, f"{args.model}_{target_kind}_{args.dtype}", args)
    lib_path = os.path.join(args.artifact_path, output_filename)
    ex.export_library(lib_path, **args.export_kwargs)
    print(f"Finish exporting to {lib_path}")


def dump_split_tir(mod: tvm.IRModule):
    template = """
from tvm.script import ir as I
from tvm.script import tir as T

# fmt: off
{content}
# fmt: on
"""
    mod_static, mod_dynamic = utils.split_static_dynamic_tir(mod)
    static_path = os.path.join(ARGS.artifact_path, "mod_tir_static.py")
    dynamic_path = os.path.join(ARGS.artifact_path, "mod_tir_dynamic.py")
    print(f"Dump static shape TIR to {static_path}")
    with open(static_path, "w") as o_f:
        o_f.write(template.format(content=mod_static.script()))
    print(f"Dump dynamic shape TIR to {dynamic_path}")
    with open(dynamic_path, "w") as o_f:
        o_f.write(template.format(content=mod_dynamic.script()))


if __name__ == "__main__":
    ARGS = _parse_args()
    os.makedirs(ARGS.artifact_path, exist_ok=True)
    os.makedirs(os.path.join(ARGS.artifact_path, "debug"), exist_ok=True)
    cache_path = os.path.join(
        ARGS.artifact_path, f"mod_cache_before_build_{ARGS.dtype}.pkl"
    )
    use_cache = ARGS.use_cache and os.path.isfile(cache_path)
    if not use_cache:
        if ARGS.model_category == "llama":
            mod, params = llama.get_model(ARGS)
        elif ARGS.model_category == "gpt_neox":
            mod, params = gpt_neox.get_model(ARGS.model, ARGS.model_path, ARGS.dtype)
        elif ARGS.model_category == "moss":
            mod, params = moss.get_model(ARGS)
        else:
            raise ValueError(f"Model {ARGS.model} not supported")
        mod = mod_transform_before_build(mod, params, ARGS)
        with open(cache_path, "wb") as outfile:
            pickle.dump(mod, outfile)
        print(f"Save a cached module to {cache_path}.")
    else:
        print(
            f"Load cached module from {cache_path} and skip tracing. "
            "You can use --use-cache=0 to retrace"
        )
        mod = pickle.load(open(cache_path, "rb"))
    dump_split_tir(mod)
    build(mod, ARGS)
