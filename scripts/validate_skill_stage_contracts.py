#!/usr/bin/env python3
"""检查或修复核心 Skill 的阶段契约一致性。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skill_runtime.stage_contract import (  # noqa: E402
    evaluate_skill_stage_contract,
    remove_undeclared_adapter_stages,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 SKILL.md、skill.yaml 与 skill.py 的阶段边界")
    parser.add_argument(
        "--skills-dir",
        type=Path,
        default=ROOT / "skill_runtime" / "skills",
        help="Skill 根目录",
    )
    parser.add_argument(
        "--fix-adapters",
        action="store_true",
        help="删除 skill.py 中未在 skill.yaml 声明的顶层阶段函数",
    )
    args = parser.parse_args()

    skill_dirs = sorted(path.parent for path in args.skills_dir.glob("*/skill.yaml"))
    if args.fix_adapters:
        for skill_dir in skill_dirs:
            removed = remove_undeclared_adapter_stages(skill_dir)
            if removed:
                print(f"已清理 {skill_dir.name}: {', '.join(removed)}")

    failures = []
    for skill_dir in skill_dirs:
        contract = evaluate_skill_stage_contract(skill_dir)
        if contract.valid:
            continue
        failures.append((skill_dir, contract.errors))

    if failures:
        for skill_dir, errors in failures:
            print(f"阶段契约失败: {skill_dir}", file=sys.stderr)
            for error in errors:
                print(f"  - {error}", file=sys.stderr)
        return 1

    print(f"阶段契约检查通过: {len(skill_dirs)} 个 Skill")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
