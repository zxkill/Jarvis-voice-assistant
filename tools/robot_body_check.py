from __future__ import annotations

"""CLI-проверка HTTP API ESP32 перед включением голосового движения.

Запуск:
    python tools/robot_body_check.py
    python tools/robot_body_check.py --base-url http://192.168.31.123 --status
    python tools/robot_body_check.py --stop

Скрипт использует те же настройки [ROBOT_BODY], что и голосовой скилл.
"""

import argparse
import json
from dataclasses import asdict

from robot.body_controller import BodyConfig, BodyController, RobotBodyError, _read_config


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка Body Control HTTP API ESP32")
    parser.add_argument("--base-url", help="Явный URL ESP32, например http://192.168.31.123")
    parser.add_argument("--status", action="store_true", help="Показать /api/status")
    parser.add_argument("--stop", action="store_true", help="Отправить /api/stop")
    parser.add_argument("--forward", type=float, help="Осторожно проехать вперёд указанное число метров")
    parser.add_argument("--backward", type=float, help="Осторожно проехать назад указанное число метров")
    parser.add_argument("--left", type=float, help="Повернуть налево на указанное число градусов")
    parser.add_argument("--right", type=float, help="Повернуть направо на указанное число градусов")
    args = parser.parse_args()

    cfg = _read_config()
    if args.base_url:
        cfg = BodyConfig(**{**asdict(cfg), "base_url": args.base_url})
    ctl = BodyController(cfg)

    try:
        if args.stop:
            print(json.dumps(ctl.stop(), ensure_ascii=False, indent=2))
        elif args.forward is not None:
            print(json.dumps(ctl.move("forward", args.forward), ensure_ascii=False, indent=2))
        elif args.backward is not None:
            print(json.dumps(ctl.move("backward", args.backward), ensure_ascii=False, indent=2))
        elif args.left is not None:
            print(json.dumps(ctl.rotate("left", args.left), ensure_ascii=False, indent=2))
        elif args.right is not None:
            print(json.dumps(ctl.rotate("right", args.right), ensure_ascii=False, indent=2))
        else:
            print(json.dumps(ctl.status(), ensure_ascii=False, indent=2))
        return 0
    except RobotBodyError as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
