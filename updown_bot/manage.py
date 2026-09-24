"""Cross-platform launcher (Windows, macOS, Linux). Needs only Python 3.11+; everything else goes in .venv.

    python manage.py setup                 create .venv, install requirements, run the tests
    python manage.py run [--fresh] [--minutes N]
    python manage.py dashboard [--port 8766]
    python manage.py report [--csv]
    python manage.py test
    python manage.py preflight             live account check: keys, wallet type, balance, positions (no orders)
    python manage.py run --live            real-money trading (also needs mode = "live" in config.toml)
    python manage.py dashboard --live      dashboard for the live/shadow ledger (data_live/)
    python manage.py certs                 macOS only: trust the keychain's roots (networks that inspect TLS)

On Windows use `py -3 manage.py ...` if `python` isn't on PATH. Any command sets up .venv first if it's missing.
"""
from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
WINDOWS = os.name == "nt"
VENV_PY = VENV / ("Scripts/python.exe" if WINDOWS else "bin/python")
SCRIPTS = {"run": "run.py", "dashboard": "dashboard.py", "report": "report.py"}
PIP_HELP = """
pip couldn't install the requirements (see the error above). Common fixes:
  * Behind a proxy: set it first, e.g.  set HTTPS_PROXY=http://proxy.company.com:8080  (Windows cmd)
                                        $env:HTTPS_PROXY="http://proxy.company.com:8080"  (PowerShell)
  * CERTIFICATE_VERIFY_FAILED: your network inspects TLS. Point pip at your company's CA file:
        set PIP_CERT=C:\\path\\to\\company-ca.pem     then rerun:  py -3 manage.py setup
    (on macOS: run  python3 manage.py certs  and use  PIP_CERT=certs/system_ca.pem)
  * No internet on this machine: on one with internet run
        python -m pip download -r requirements.txt --platform win_amd64 --only-binary=:all: -d wheels
    copy the wheels folder over, then:  .venv\\Scripts\\python -m pip install --no-index --find-links wheels -r requirements.txt
"""


def check_python() -> None:
    if sys.version_info < (3, 11):
        sys.exit(f"Python 3.11+ is required (this is {sys.version.split()[0]}). Get it from https://www.python.org/downloads/")


def call(args: list[str]) -> int:
    """Run a child process in the project folder; Ctrl+C goes to the child, which shuts down cleanly."""
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(args, cwd=HERE, env=env)
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            continue  # the child got the same Ctrl+C; wait for it to save state and exit


def setup(run_tests: bool = True) -> None:
    if not VENV_PY.exists():
        print(f"Creating virtual environment in {VENV} ...")
        venv.EnvBuilder(with_pip=True).create(VENV)
    # pip >= 24.2 verifies TLS against the OS certificate store (truststore), which is what makes installs work
    # behind corporate proxies on Windows; the pip bundled with older Pythons doesn't, so upgrade it first.
    print("Updating pip ...")
    call([str(VENV_PY), "-m", "pip", "install", "--disable-pip-version-check", "-q", "--upgrade", "pip"])
    print("Installing requirements ...")
    if call([str(VENV_PY), "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", "requirements.txt"]):
        sys.exit(PIP_HELP)
    if run_tests:
        test()
    print("\nReady. Start the bot with:  python manage.py run --fresh\nDashboard:                  python manage.py dashboard")


def ensure_venv() -> None:
    if not VENV_PY.exists():
        setup(run_tests=False)


def test() -> None:
    ensure_venv()
    code = call([str(VENV_PY), "-m", "unittest", "-v", "tests.test_core"])
    if code:
        sys.exit(code)


def certs() -> None:
    """Export macOS keychain roots (incl. a corporate proxy CA) to certs/system_ca.pem; config.toml points at it.
    Windows and Linux don't need this: the bot already trusts the OS certificate store."""
    if sys.platform != "darwin":
        print("Not needed on this OS: the bot trusts the system certificate store (Windows: includes corporate roots).")
        return
    out = HERE / "certs" / "system_ca.pem"
    out.parent.mkdir(exist_ok=True)
    pem = subprocess.run(["security", "find-certificate", "-a", "-p", "/Library/Keychains/System.keychain",
                          "/System/Library/Keychains/SystemRootCertificates.keychain"],
                         capture_output=True, text=True, check=True).stdout
    out.write_text(pem, encoding="utf-8")
    print(f"Wrote {pem.count('BEGIN CERTIFICATE')} certificates to {out}")


def main() -> None:
    check_python()
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "setup":
        setup()
    elif cmd == "test":
        test()
    elif cmd == "certs":
        certs()
    elif cmd == "preflight":
        ensure_venv()
        sys.exit(call([str(VENV_PY), "run.py", "--preflight", *rest]))
    elif cmd in SCRIPTS:
        ensure_venv()
        sys.exit(call([str(VENV_PY), SCRIPTS[cmd], *rest]))
    else:
        sys.exit(f"Unknown command {cmd!r}. Run `python manage.py help`.")


if __name__ == "__main__":
    main()
