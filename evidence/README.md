# evidence/

Filled in by `python verify_startup.py` (run it once with the real packages installed, then submit this folder):

* `startup_evidence.json` - every check, PIDs of the 5 processes, primitives listed over the wire, the live Showcase result, the live Stress Test 4 result
* `pytest_output.txt` - verbose output of the executed test suite
* `<server>.stdout.txt` - each process's console output

Per-process JSON debug logs are written to `logs/<server>.jsonl`.
