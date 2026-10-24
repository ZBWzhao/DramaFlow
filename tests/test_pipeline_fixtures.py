"""阶段 0 端到端产物固化：管线各层 JSON 真实存在且可解析。"""
import json
from pathlib import Path

import pytest

from core.pipeline_artifacts import ARTIFACTS

RUN1 = Path(__file__).resolve().parent / "fixtures" / "pipeline_run1"


def test_sample_novel_exists():
    novel = Path(__file__).resolve().parent / "fixtures" / "sample_novel_v1.txt"
    text = novel.read_text(encoding="utf-8")
    assert len(text) >= 2000, "样例小说太短，撑不起改编管线"


@pytest.mark.parametrize("art", ARTIFACTS, ids=lambda a: a.name)
def test_run1_artifact_json_parses(art):
    path = RUN1 / art.filename
    assert path.is_file(), f"{art.name}: 缺 {art.filename}（character_refs 若仅有提示词产物见 README）"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, (dict, list)), f"{art.name}: 产物不是 JSON 对象/数组"
