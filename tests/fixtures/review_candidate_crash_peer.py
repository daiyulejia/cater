"""Hold a candidate lease in a disposable subprocess until terminated."""
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / 'tests'))

from cater.runtime import Runtime
from test_team_external import ScriptedExternalDriver


def main():
    db, roles, task, seq, digest, ready, mode = sys.argv[1:]
    driver = ScriptedExternalDriver()
    rt = Runtime(db, roles_path=roles, drivers={driver.name: driver})
    try:
        with rt.team.recovery.review_candidate_lease(task, candidate_seq=int(seq), candidate_hash=digest) as claimed:
            if claimed is None:
                raise RuntimeError('candidate not claimed')
            if mode == 'unknown':
                rt.team.external_runs.begin(task, claimed[1], claimed[2], basis={},
                    binding=rt.workspaces.view(task), tool_names=[], driver_fingerprint='offline-fixture')
            Path(ready).write_text(str(claimed[2].seq), encoding='utf-8')
            sys.stdin.buffer.read(1)
    finally:
        rt.close()


if __name__ == '__main__':
    main()
