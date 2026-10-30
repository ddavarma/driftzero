"""
verify_providers.py  -  30-second smoke test for every credit pool.

Run from the repo root after filling .env:
    pip install openai python-dotenv modal
    python verify_providers.py

It makes ONE tiny call per provider (a few hundred tokens total) and
prints PASS/FAIL per line. Nothing here is a guess: if a model id is
wrong for your account, the provider's error message will say so, and
the Meta section also prints the exact model ids your key can see.
"""
import os
import shutil
import subprocess
import sys

from dotenv import load_dotenv

load_dotenv()

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


def report(name, status, detail=""):
    print(f"[{status}] {name}" + (f"  ->  {detail}" if detail else ""))


def openai_client(base_url, api_key):
    from openai import OpenAI
    return OpenAI(base_url=base_url, api_key=api_key)


# ---------------------------------------------------------------- Meta Model API
def check_meta():
    key = os.getenv("META_API_KEY") or os.getenv("MODEL_API_KEY")
    if not key:
        return report("Meta Model API", SKIP, "META_API_KEY not set")
    try:
        c = openai_client("https://api.meta.ai/v1", key)
        # 1) list the model ids this key can actually use (do not trust any chat log for these)
        ids = sorted(m.id for m in c.models.list().data)
        report("Meta /v1/models", PASS, ", ".join(ids))
        # 2) one real completion
        r = c.chat.completions.create(
            model="muse-spark-1.3",
            messages=[{"role": "user", "content": "Reply with the single word OK."}],
            max_tokens=20,
        )
        report("Meta muse-spark-1.3 chat", PASS, repr(r.choices[0].message.content))
    except Exception as e:  # noqa: BLE001
        report("Meta Model API", FAIL, str(e)[:300])


# ---------------------------------------------------------------- Baseten
def check_baseten():
    key = os.getenv("BASETEN_API_KEY")
    if not key:
        return report("Baseten", SKIP, "BASETEN_API_KEY not set")
    model = os.getenv("BASETEN_MODEL", "meta-models/muse-spark-open-1026")
    try:
        c = openai_client("https://inference.baseten.co/v1", key)
        r = c.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with the single word OK."}],
            max_tokens=200,  # model always reasons first; leave headroom
            extra_body={"reasoning_effort": "low"},
        )
        msg = r.choices[0].message
        has_reasoning = bool(getattr(msg, "reasoning_content", None))
        report(f"Baseten {model}", PASS,
               f"content={msg.content!r} reasoning_content_present={has_reasoning}")
    except Exception as e:  # noqa: BLE001
        report("Baseten", FAIL, str(e)[:300])


# ---------------------------------------------------------------- Fireworks
def check_fireworks():
    key = os.getenv("FIREWORKS_API_KEY")
    if not key:
        return report("Fireworks", SKIP, "FIREWORKS_API_KEY not set")
    model = os.getenv("FIREWORKS_MODEL", "accounts/fireworks/models/muse-spark-open")
    try:
        c = openai_client("https://api.fireworks.ai/inference/v1", key)
        r = c.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with the single word OK."}],
            max_tokens=200,
        )
        report(f"Fireworks {model}", PASS, repr(r.choices[0].message.content))
    except Exception as e:  # noqa: BLE001
        report("Fireworks", FAIL, str(e)[:300])


# ---------------------------------------------------------------- Modal
def check_modal():
    try:
        import modal
    except ImportError:
        return report("Modal", SKIP, "pip install modal")
    try:
        prof = subprocess.run(["modal", "profile", "current"], capture_output=True, text=True)
        report("Modal active profile", PASS, prof.stdout.strip() or prof.stderr.strip())
        app = modal.App.lookup("driftzero-smoke", create_if_missing=True)
        sb = modal.Sandbox.create(image=modal.Image.debian_slim(python_version="3.11"),
                                  app=app, timeout=60)
        p = sb.exec("python", "-c", "print('sandbox ok')")
        p.wait()
        out = p.stdout.read().strip()
        sb.terminate()
        report("Modal Sandbox exec", PASS if out == "sandbox ok" else FAIL, out)
    except Exception as e:  # noqa: BLE001
        report("Modal", FAIL, str(e)[:300])


# ---------------------------------------------------------------- Muse Code CLI
def check_muse_cli():
    if not shutil.which("muse"):
        return report("Muse Code CLI", FAIL, "`muse` not on PATH (is ~/.local/bin in PATH?)")
    v = subprocess.run(["muse", "--version"], capture_output=True, text=True)
    report("Muse Code CLI", PASS, (v.stdout or v.stderr).strip())


# ---------------------------------------------------------------- Claude Code CLI
def check_claude_cli():
    if not shutil.which("claude"):
        return report("Claude Code CLI", FAIL, "`claude` not on PATH")
    v = subprocess.run(["claude", "--version"], capture_output=True, text=True)
    report("Claude Code CLI", PASS, (v.stdout or v.stderr).strip())


if __name__ == "__main__":
    print(f"python {sys.version.split()[0]}\n")
    check_meta()
    check_baseten()
    check_fireworks()
    check_modal()
    check_muse_cli()
    check_claude_cli()
