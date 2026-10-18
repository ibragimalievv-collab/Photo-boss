"""Course compatibility and real lesson asset contracts, without network calls."""
import xml.etree.ElementTree as ET
from pathlib import Path

from app.services.academy import ACADEMY_BLOCKS, ACADEMY_LESSONS
from app.services.academy_materials import LESSON_MATERIALS

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SLUGS = (
    "pose", "light", "color", "client", "portrait-angle", "portrait-hands",
    "portrait-body", "portrait-character", "couple-distance", "couple-action",
    "couple-levels", "couple-story", "family-triangle", "family-contact",
    "family-body-types", "family-series", "child-safety", "child-eye-level",
    "child-motion", "child-emotion", "group-layout", "group-attention",
    "location-light", "group-variety", "body-respect", "body-angle", "selection",
    "final-standard",
)


def test_expansion_preserves_saved_course_progress_keys_and_bot_message_size():
    assert tuple(lesson.slug for lesson in ACADEMY_LESSONS) == EXPECTED_SLUGS
    assert set(LESSON_MATERIALS) == set(EXPECTED_SLUGS)
    assert len(ACADEMY_BLOCKS) == 7
    for day, lesson in enumerate(ACADEMY_LESSONS, 1):
        assert lesson.day == day
        assert lesson.block == (day - 1) // 4 + 1
        assert 180 <= len(lesson.body.split()) <= 320
        assert len(lesson.body) < 3800  # Leaves room for the Telegram lesson header.
        data = LESSON_MATERIALS[lesson.slug]
        assert len(data["steps"]) >= 3 and len(data["successChecks"]) >= 2
        assert data["mistake"]["problem"] and data["mistake"]["fix"]


def test_every_lesson_reference_resolves_to_a_real_authenticated_training_asset():
    directories = {"woman": "woman", "man": "man", "couple": "couple", "child": "child",
                   "family_portrait": "family-portrait", "adults": "adult-group",
                   "plus_size": "plus-size", "slim": "slim"}
    for data in LESSON_MATERIALS.values():
        assert 1 <= len(data["references"]) <= 2
        for ref in data["references"]:
            assert ref["path"].startswith("/academy/practice/references/")
            category, index = ref["path"].rsplit("/", 2)[-2:]
            assert 1 <= int(index) <= 5
            asset = ROOT / "app/assets/training" / directories[category] / f"{int(index):02}.jpg"
            assert asset.is_file(), str(asset)
            assert ref["caption"].startswith("Учебный пример.")
            assert ref["alt"]


def test_each_linked_teaching_diagram_is_local_valid_accessible_svg():
    linked = {d["path"] for item in LESSON_MATERIALS.values() for d in item["diagrams"]}
    assert len(linked) == 4
    for path in linked:
        asset = ROOT / "app/webapp" / path.removeprefix("/app/")
        svg = ET.parse(asset).getroot()
        assert svg.tag == "{http://www.w3.org/2000/svg}svg"
        assert svg.find("{http://www.w3.org/2000/svg}title") is not None
        assert svg.find("{http://www.w3.org/2000/svg}desc") is not None
        assert not any(node.tag.endswith("script") for node in svg.iter())
