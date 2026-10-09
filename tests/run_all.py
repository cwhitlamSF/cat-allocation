"""Run every test script; print ok / FAIL with the tail of the output for failures."""
import glob, os, subprocess, sys
here = os.path.dirname(os.path.abspath(__file__))
failed = 0
for t in sorted(glob.glob(os.path.join(here, 'test_*.py'))):
    p = subprocess.run([sys.executable, t], capture_output=True, text=True, cwd=here)
    ok = p.returncode == 0
    failed += not ok
    print(f"{os.path.basename(t):<26} {'ok' if ok else 'FAIL'}")
    if not ok:
        print((p.stdout + p.stderr)[-2000:])
sys.exit(1 if failed else 0)
