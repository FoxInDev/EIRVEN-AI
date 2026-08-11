from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from eirven_ai.video import VideoEditor


def settings(root: Path) -> SimpleNamespace:
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(root_dir=root, data_dir=data, fast_model="test")


def fake_probe(_path: Path, *, verify_frame: bool = False) -> dict[str, object]:
    return {
        "duration": 30.0,
        "width": 1280,
        "height": 720,
        "codec": "h264",
        "has_audio": True,
    }


class DummyContext:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.total = 0

    def set_total(self, value: int) -> None:
        self.total = value

    def update(self, message: str, **_kwargs: object) -> None:
        self.messages.append(message)

    def check_cancelled(self) -> None:
        return None


def test_video_help_is_explicit_and_does_not_steal_playback(tmp_path: Path) -> None:
    editor = VideoEditor(settings(tmp_path), start_watcher=False)
    editor.open_inbox = lambda: {"opened": True, "path": str(editor.inbox)}  # type: ignore[method-assign]

    assert not editor.is_relevant("Открой YouTube и включи любое видео", "chat")
    result = editor.handle_query("Эрви, как мне смонтировать видео?", "chat")

    assert result["route"]["action"] == "video_help"
    assert "Я умею монтировать" in result["answer"]
    assert "папку video" in result["answer"]
    assert "1.mp4" in result["answer"]


def test_asr_typo_in_how_to_question_still_opens_video_help(tmp_path: Path) -> None:
    editor = VideoEditor(settings(tmp_path), start_watcher=False)
    editor.open_inbox = lambda: {"opened": True, "path": str(editor.inbox)}  # type: ignore[method-assign]

    result = editor.handle_query("ка смонтировать видео", "chat")

    assert result["route"]["action"] == "video_help"
    assert "сама сразу переименую" in result["answer"]


def test_completed_copies_are_auto_numbered_without_a_montage_command(tmp_path: Path) -> None:
    editor = VideoEditor(settings(tmp_path), start_watcher=False)
    (editor.inbox / "z-duejebdvdhb2.mov").write_bytes(b"z")
    (editor.inbox / "a-random-name.mp4").write_bytes(b"a")

    first_poll = editor._number_stable_inbox(now=10.0)
    second_poll = editor._number_stable_inbox(now=11.0)

    assert first_poll["copying"] is True
    assert second_poll["renamed"] is True
    assert sorted(path.name for path in editor.inbox.iterdir() if path.suffix) == ["1.mp4", "2.mov"]
    originals = editor._state["inbox_originals"]
    assert originals["1.mp4"]["original_name"] == "a-random-name.mp4"
    assert originals["2.mov"]["original_name"] == "z-duejebdvdhb2.mov"

    editor.open_inbox = lambda: {"opened": True, "path": str(editor.inbox)}  # type: ignore[method-assign]
    ready = editor.handle_query("Я положил все видео в папку", "chat")
    assert ready["route"]["action"] == "video_files_ready"
    assert "1.mp4, 2.mov" in ready["answer"]
    located = editor.handle_query("Они находятся в папке", "chat")
    assert located["route"]["action"] == "video_files_ready"


def test_log_phrase_understands_spoken_seconds_spaced_1080_and_mp4_alias(tmp_path: Path) -> None:
    inbox = tmp_path / "video"
    inbox.mkdir()
    (inbox / "second.mp4").write_bytes(b"2")
    (inbox / "first.mp4").write_bytes(b"1")
    editor = VideoEditor(settings(tmp_path), start_watcher=False)
    editor._probe = fake_probe  # type: ignore[method-assign]

    result = editor.handle_query(
        "мне нужно чтобы ты из каждого видео убрала первые три секунды склеила их по порядку "
        "сделала качество 1 080 увеличила чуть чуть резкость и итоговое видео сделала расширением по4",
        "chat",
    )

    assert result["route"]["action"] == "video_enqueue"
    plan = result["enqueue"]["plan"]
    assert plan["join"] is True
    assert plan["resolution"] == [1920, 1080]
    assert plan["enhance"] is True
    assert plan["format"] == "mp4"
    assert plan["clips"]["1"]["start"] == 3.0
    assert plan["clips"]["2"]["start"] == 3.0


def test_short_all_videos_fragment_is_kept_for_the_next_voice_phrase(tmp_path: Path) -> None:
    inbox = tmp_path / "video"
    inbox.mkdir()
    (inbox / "one.mp4").write_bytes(b"1")
    (inbox / "two.mp4").write_bytes(b"2")
    editor = VideoEditor(settings(tmp_path), start_watcher=False)
    editor._probe = fake_probe  # type: ignore[method-assign]

    first = editor.handle_query("У всех видео,-ээ.", "chat")
    second = editor.handle_query("Убери первые три секунды и склей по порядку", "chat")

    assert first["route"]["action"] == "video_clarification"
    assert "применить действие ко всем" in first["answer"]
    assert second["route"]["action"] == "video_enqueue"
    assert second["enqueue"]["plan"]["clips"]["1"]["start"] == 3.0
    assert second["enqueue"]["plan"]["clips"]["2"]["start"] == 3.0


@pytest.mark.parametrize(
    ("answer", "expected_resolution"),
    [
        ("э э первый вариант", [1920, 1080]),
        ("повысь резкость без изменений", None),
    ],
)
def test_quality_clarification_accepts_natural_log_answers(
    tmp_path: Path, answer: str, expected_resolution: list[int] | None
) -> None:
    inbox = tmp_path / "video"
    inbox.mkdir()
    (inbox / "clip.mp4").write_bytes(b"x")
    editor = VideoEditor(settings(tmp_path), start_watcher=False)
    editor._probe = fake_probe  # type: ignore[method-assign]

    first = editor.handle_query("Улучши качество видео", "chat")
    second = editor.handle_query(answer, "chat")

    assert first["route"]["action"] == "video_clarification"
    assert second["route"]["action"] == "video_enqueue"
    assert second["enqueue"]["plan"]["resolution"] == expected_resolution
    assert second["enqueue"]["plan"]["enhance"] is True


@pytest.mark.parametrize("phrase", ["принимай монтаж", "ну мне всё нравится"])
def test_rendered_project_accepts_natural_log_phrases(tmp_path: Path, phrase: str) -> None:
    editor = VideoEditor(settings(tmp_path), start_watcher=False)
    result_file = editor.inbox / "result.mp4"
    result_file.write_bytes(b"ready")
    editor._state["active"] = {
        "id": "20260811-120000-abcdef",
        "conversation_id": "chat",
        "status": "rendered",
        "sources": [],
        "outputs": [{"path": str(result_file), "name": result_file.name}],
    }
    editor._save_state()

    accepted = editor.handle_query(phrase, "chat")

    assert accepted["route"]["action"] == "video_accept"
    assert "Приняла монтаж" in accepted["answer"]
    assert not result_file.exists()


def test_mixed_files_are_numbered_and_ambiguous_scope_is_clarified(tmp_path: Path) -> None:
    inbox = tmp_path / "video"
    inbox.mkdir()
    (inbox / "z-last.mov").write_bytes(b"z")
    (inbox / "a-first.mp4").write_bytes(b"a")
    editor = VideoEditor(settings(tmp_path), start_watcher=False)
    editor._probe = fake_probe  # type: ignore[method-assign]

    first = editor.handle_query("Эрви, обрезать с первых 5 секунд и склеить", "chat")

    assert first["route"]["action"] == "video_clarification"
    assert "Что именно обрезать" in first["answer"]
    assert sorted(path.name for path in inbox.iterdir()) == ["1.mp4", "2.mov"]
    active = editor.status()["active"]
    assert [(item["name"], item["original_name"]) for item in active["sources"]] == [
        ("1.mp4", "a-first.mp4"),
        ("2.mov", "z-last.mov"),
    ]

    second = editor.handle_query("Только у первого видео", "chat")

    assert second["route"]["action"] == "video_enqueue"
    plan = second["enqueue"]["plan"]
    assert plan["render_indices"] == [1, 2]
    assert plan["clips"]["1"]["start"] == 5.0
    assert plan["clips"]["2"] == {}


def test_quality_request_collects_target_before_render(tmp_path: Path) -> None:
    inbox = tmp_path / "video"
    inbox.mkdir()
    (inbox / "clip.mkv").write_bytes(b"x")
    editor = VideoEditor(settings(tmp_path), start_watcher=False)
    editor._probe = fake_probe  # type: ignore[method-assign]

    first = editor.handle_query("Улучши качество видео", "chat")
    assert first["route"]["action"] == "video_clarification"
    assert "1080p" in first["answer"] and "4K" in first["answer"]

    second = editor.handle_query("Сделай 1080p", "chat")
    assert second["route"]["action"] == "video_enqueue"
    assert second["enqueue"]["plan"]["resolution"] == [1920, 1080]
    assert second["enqueue"]["plan"]["enhance"] is True


def test_new_files_do_not_silently_join_active_project(tmp_path: Path) -> None:
    inbox = tmp_path / "video"
    inbox.mkdir()
    (inbox / "one.mp4").write_bytes(b"1")
    editor = VideoEditor(settings(tmp_path), start_watcher=False)
    editor._probe = fake_probe  # type: ignore[method-assign]

    prepared = editor.handle_query("Конвертируй видео в mp4", "chat")
    assert prepared["route"]["action"] == "video_enqueue"
    (inbox / "new-source.mov").write_bytes(b"2")

    next_request = editor.handle_query("Склей все видео", "chat")
    assert next_request["route"]["action"] == "video_clarification"
    assert "не смешиваю" in next_request["answer"]


def test_unreadable_source_is_rejected_without_losing_original_name(tmp_path: Path) -> None:
    inbox = tmp_path / "video"
    inbox.mkdir()
    original = inbox / "family-memory.mov"
    original.write_bytes(b"broken")
    editor = VideoEditor(settings(tmp_path), start_watcher=False)

    def broken_probe(_path: Path, *, verify_frame: bool = False) -> dict[str, object]:
        raise RuntimeError("повреждённый контейнер")

    editor._probe = broken_probe  # type: ignore[method-assign]
    result = editor.handle_query("Сделай видео в mp4", "chat")

    assert result["route"]["action"] == "video_clarification"
    assert "повреждённый контейнер" in result["answer"]
    assert original.is_file()
    assert not (inbox / "1.mov").exists()


def test_follow_up_adjustment_keeps_previous_join_structure(tmp_path: Path) -> None:
    inbox = tmp_path / "video"
    inbox.mkdir()
    (inbox / "one.mp4").write_bytes(b"1")
    (inbox / "two.mov").write_bytes(b"2")
    editor = VideoEditor(settings(tmp_path), start_watcher=False)
    editor._probe = fake_probe  # type: ignore[method-assign]

    first = editor.handle_query("Склей все видео", "chat")
    assert first["enqueue"]["plan"]["join"] is True
    editor._state["active"]["status"] = "rendered"
    editor._save_state()

    revision = editor.handle_query("Сделай немного громче", "chat")
    plan = revision["enqueue"]["plan"]
    assert plan["join"] is True
    assert plan["render_indices"] == [1, 2]
    assert plan["volume"] == pytest.approx(1.2)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg is unavailable")
def test_real_join_verification_and_acceptance_archive(tmp_path: Path) -> None:
    inbox = tmp_path / "video"
    inbox.mkdir()
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    subprocess.run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=red:s=320x240:d=0.45",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.45",
            "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(inbox / "b.mov"),
        ],
        check=True,
    )
    # The second clip deliberately has no audio. The render lane must add silence so
    # concat keeps one stable A/V stream layout instead of dropping or desynchronising.
    subprocess.run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=blue:s=640x360:d=0.45",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(inbox / "a.mp4"),
        ],
        check=True,
    )
    editor = VideoEditor(settings(tmp_path), start_watcher=False)

    prepared = editor.handle_query("Эрви, склей все видео по порядку", "chat")
    assert prepared["route"]["action"] == "video_enqueue"
    rendered = editor.render(DummyContext(), prepared["enqueue"])

    output = Path(rendered["outputs"][0]["path"])
    assert output.name == "result.mp4"
    assert output.stat().st_size > 1_000
    assert rendered["outputs"][0]["duration"] > .7
    assert rendered["outputs"][0]["has_audio"] is True

    accepted = editor.handle_query("Эрви, принимаю монтаж", "chat")
    assert accepted["route"]["action"] == "video_accept"
    assert not [path for path in inbox.iterdir() if path.suffix.casefold() in {".mp4", ".mov"}]
    assert len(list((tmp_path / "video_results").glob("*.mp4"))) == 1
    assert len(list((tmp_path / "video_archive").glob("*/sources/*"))) == 2
