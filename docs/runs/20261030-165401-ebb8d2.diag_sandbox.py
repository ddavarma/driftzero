"""Diagnose: same image + same install command as driftzero/verify.py, verbose, then probe imports."""
import io, sys, tarfile, modal
from pathlib import Path
wt = Path(sys.argv[1])
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w:gz") as t:
    t.add(wt, arcname="wt", filter=lambda i: None if any(p in (".git", "__pycache__") for p in Path(i.name).parts) else i)
image = modal.Image.debian_slim(python_version="3.11").pip_install("pytest", "numpy", "setuptools", "wheel")
app = modal.App.lookup("driftzero-verify", create_if_missing=True)
sb = modal.Sandbox.create(app=app, image=image, timeout=600, workdir="/work")
try:
    sb.filesystem.write_bytes(buf.getvalue(), "/tmp/wt.tar.gz")
    def run(*cmd):
        p = sb.exec(*cmd, workdir="/work"); out, err = p.stdout.read(), p.stderr.read(); code = p.wait()
        print(f"$ {' '.join(cmd)}\n# exit {code}\n{(out + err)[-2500:]}\n")
    run("tar", "xzf", "/tmp/wt.tar.gz", "-C", "/work")
    run("python", "-m", "pip", "install", "--no-build-isolation", "-e", "/work/wt/.")
    run("python", "-c", "import importlib.util as u; print({m: bool(u.find_spec(m)) for m in ('openai','httpx','tiktoken','jobtracker','JobTracker')})")
    run("python", "-m", "pip", "show", "jobtracker")
finally:
    sb.terminate()
