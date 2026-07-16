from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voicetype_local.ui import make_tray_image


target = Path("assets/voicetype.ico")
target.parent.mkdir(parents=True, exist_ok=True)
make_tray_image("#2563EB").save(target, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
print(target.resolve())
