"""Create local configurations without overwriting existing files."""
from pathlib import Path
import argparse
import json
import shutil

ROOT = Path(__file__).resolve().parents[1]
NAMES = ['deployment', 'model_source', 'sampling_chat', 'sampling_eval', 'persona_state']

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    pairs = [(ROOT / 'configs' / (name + '.example.json'), ROOT / 'configs' / (name + '.json')) for name in NAMES]
    pairs.append((ROOT / 'configs/persona.example.md', ROOT / 'configs/persona.md'))
    for source, target in pairs:
        if source.suffix == '.json':
            json.loads(source.read_text(encoding='utf-8-sig'))
        if target.exists():
            print('keep:', target.relative_to(ROOT))
        elif args.dry_run:
            print('would create:', target.relative_to(ROOT))
        else:
            # Exclusive create avoids overwriting a concurrent first launch.
            try:
                with target.open('xb') as stream:
                    stream.write(source.read_bytes())
            except FileExistsError:
                print('keep:', target.relative_to(ROOT))
            else:
                print('created:', target.relative_to(ROOT))

if __name__ == '__main__':
    main()
