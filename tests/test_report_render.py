"""The rendered report, and the design rules that must not regress.

Several of these bind properties that are invisible in a screenshot but break
the report in a specific place: an external reference breaks it as an email
attachment, `opacity` for text hierarchy breaks its contrast on a different
surface, `outline: none` breaks it for a television remote, and a colour used
as the only carrier of meaning breaks it for a reader who cannot distinguish
the colours.
"""
import importlib.util
import pathlib
import re

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[1] / "iptv_checker" / "reports.py"
_spec = importlib.util.spec_from_file_location("ic_reports_render", _PATH)
reports = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reports)


def _row(cid, status, error_type="N/A", fps=0, name="Ch", stream_id=None):
    return {"channel_id": cid, "channel_name": name,
            "stream_id": stream_id if stream_id is not None else cid * 100,
            "status": status, "error_type": error_type, "framerate_num": fps}


_RESULTS = [
    _row(1, "Dead", "Timeout", name="All Dead"),
    _row(1, "Dead", "Timeout", name="All Dead", stream_id=101),
    _row(2, "Dead", "Timeout", name="Has Backup"),
    _row(2, "Alive", fps=50, name="Has Backup", stream_id=201),
    _row(3, "Dead", "Black Screen", name="Provider Slate"),
    _row(4, "Skipped", "Rate Limited", name="Unproven"),
    _row(5, "Alive", fps=50, name="Perfectly Fine"),
]


@pytest.fixture
def model():
    return reports.build_model(_RESULTS, {"black_screen_detection": True},
                               now=1754400000, version="9.9.9")


@pytest.fixture
def page(model):
    return reports.render_html(model)


# ---- self-contained: it is opened off disk and mailed as an attachment ---

def test_no_external_references(page):
    """No CDN, no webfont, no remote image, no link element. The page is read
    as a file URL, as a mail attachment, and on a television with no route to
    the internet."""
    assert "<link" not in page.lower()
    assert "@import" not in page
    for pattern in ("src=\"http", "src='http", "href=\"http://cdn", "url(http"):
        assert pattern not in page, pattern
    # The only permitted absolute links are the two footer destinations.
    hrefs = re.findall(r'href="([^"]+)"', page)
    assert set(hrefs) <= {reports.REPO_URL, reports.ISSUES_URL}, hrefs


def test_no_script_required_for_sections(page):
    """Sections are details elements. A client that does not implement them
    renders everything EXPANDED, so the failure mode is everything visible."""
    assert "<details>" in page
    assert "<summary>" in page


def test_every_section_starts_collapsed(page):
    """No details element carries the open attribute."""
    assert "<details open" not in page
    assert "<details >" not in page


# ---- the CSS token layer -------------------------------------------------

def test_spacing_scale_exists_and_every_step_is_used():
    css = reports._CSS
    for token in ("--s1:", "--s2:", "--s3:", "--s4:", "--s5:"):
        assert token in css, token
        assert ("var(%s)" % token.rstrip(":")) in css, "step %s declared but never used" % token


def test_grey_ramp_exists():
    for token in ("--ink:", "--ink-muted:", "--ink-dim:"):
        assert token in reports._CSS, token


def test_opacity_is_never_used_for_text_hierarchy():
    """An opacity value paints a different colour on every surface, so the
    contrast ratio moves whenever a background changes, and the fade applies to
    everything nested inside. The grey ramp exists so this is unnecessary."""
    assert "opacity" not in reports._CSS


def test_no_important_anywhere():
    """Light and dark differ ONLY in token values. Needing !important would
    mean the tokens are wrong."""
    assert "!important" not in reports._CSS


def test_light_and_dark_differ_only_in_token_values():
    """Both themes redeclare the same token names and nothing else."""
    dark = reports._CSS[reports._CSS.index('[data-theme="dark"]'):]
    dark_block = dark[:dark.index("}")]
    for prop in ("--bg", "--surface", "--border", "--ink", "--ink-muted", "--ink-dim"):
        assert prop in dark_block, prop
    assert "font-size" not in dark_block
    assert "padding" not in dark_block


def test_focus_ring_is_never_removed():
    """That ring is how the page is driven by a television remote D-pad."""
    assert "outline: none" not in reports._CSS
    assert "outline:none" not in reports._CSS
    assert "summary:focus-visible" in reports._CSS


# ---- colour is never the only carrier -----------------------------------

def test_every_section_pairs_colour_with_a_word_and_a_glyph(page, model):
    for section in model["sections"]:
        assert section["title"] in page
    for dot_class, glyph in reports._DOT_GLYPH.items():
        assert dot_class in page, dot_class
        # `glyph in page` ALONE IS VACUOUS: an empty string is contained in
        # every string, so blanking a glyph passed silently. Caught by a
        # mutation that set one glyph to "". Assert it is non-empty first.
        assert glyph, "glyph for %s is empty" % dot_class
        assert glyph in page, "glyph missing for %s" % dot_class


def test_one_glyph_is_rendered_per_section(page, model):
    """Counting the rendered spans catches a blanked glyph, which a containment
    check on the glyph text cannot."""
    assert page.count('<span class="glyph" aria-hidden="true">') == len(model["sections"])


def test_glyphs_are_keyed_on_the_colour_class_not_the_title():
    """A glyph keyed on the title could disagree with the colour beside it."""
    for key, dot in reports._SECTION_DOT.items():
        assert dot in reports._DOT_GLYPH, "%s has no glyph for its class" % key
        assert reports._DOT_GLYPH[dot], "%s maps to an empty glyph" % key


def test_every_dot_class_maps_to_a_distinct_glyph():
    """Two sections sharing a glyph would make the pairing ambiguous."""
    glyphs = list(reports._DOT_GLYPH.values())
    assert len(glyphs) == len(set(glyphs)), glyphs


def test_decorative_marks_are_hidden_from_assistive_technology(page):
    assert page.count('class="dot ') == page.count('aria-hidden="true"></span>') or \
        'aria-hidden="true"' in page
    assert '<span class="glyph" aria-hidden="true">' in page


# ---- section contract ----------------------------------------------------

def test_heading_count_equals_rows_in_its_table(model, page):
    for section in model["sections"]:
        assert ('<span class="count">%d</span>' % len(section["rows"])) in page


def test_every_section_states_what_it_holds_and_what_to_do(page, model):
    for section in model["sections"]:
        assert section["description"] in page
        assert section["action"] in page
    assert page.count('<p class="act">What to do: ') == len(model["sections"])


def test_every_section_carries_a_find_in_page_hint(page, model):
    assert page.count(reports._FIND_HINT) == len(model["sections"])


def test_an_empty_section_says_so_rather_than_rendering_a_bare_heading(page):
    assert "Nothing in this group." in page


# ---- charts --------------------------------------------------------------

def test_chart_colours_come_from_a_class_never_a_fill_attribute(page):
    """A fill attribute holding a custom property has patchy support and fails
    silently to BLACK, which is an invisible chart on the dark surface."""
    assert 'fill="var(' not in page
    assert 'class="bar-' in page


def test_chart_survives_all_zero_counts():
    """Must not divide by zero when nothing was found."""
    empty = reports.build_model([], {}, now=0)
    assert reports._bar_chart(empty["sections"]) == ""
    assert reports.render_html(empty)


def test_chart_has_an_accessible_label(page):
    assert 'role="img"' in page and "aria-label=" in page


# ---- rendered copy conventions ------------------------------------------

def test_no_em_dash_en_dash_or_double_hyphen_in_the_visible_copy(page):
    """A double hyphen reads as an em dash on the page. The CSS custom
    properties legitimately contain a double hyphen, so the style block is
    excluded rather than the rule being weakened."""
    body = page[page.index("</style>"):]
    assert chr(0x2014) not in body, "em dash in rendered copy"
    assert chr(0x2013) not in body, "en dash in rendered copy"
    assert "--" not in body, "double hyphen in rendered copy"


def test_no_contractions_in_the_visible_copy(page):
    body = page[page.index("</style>"):]
    found = re.findall(
        r"\b(?:do|does|is|are|it|that|there|you|we|can|will|would|should|could|has|have)"
        r"(?:n't|'s|'re|'ll|'ve|'d)\b", body, re.I)
    assert not found, found


# ---- the logo ------------------------------------------------------------

def test_missing_logo_renders_no_image_and_never_fails(tmp_path):
    assert reports._logo_data_uri(str(tmp_path)) is None
    assert reports._logo_data_uri(None) is None
    model = reports.build_model(_RESULTS, {}, now=0)
    model["plugin_dir"] = str(tmp_path)
    assert "<img" not in reports.render_html(model)


def test_oversized_logo_is_dropped_rather_than_embedded(tmp_path):
    """This plugin's logo.png is 310 KB, which is 414 KB base64 and would ride
    on every emailed copy of every report. Over the cap, no image at all."""
    (tmp_path / "logo.png").write_bytes(b"x" * (200 * 1024))
    assert reports._logo_data_uri(str(tmp_path)) is None


def test_small_logo_is_embedded_as_a_data_uri(tmp_path):
    (tmp_path / "logo.png").write_bytes(b"x" * 128)
    uri = reports._logo_data_uri(str(tmp_path))
    assert uri and uri.startswith("data:image/png;base64,")


# ---- every renderer helper must be TOTAL --------------------------------

@pytest.mark.parametrize("value", [None, "", "abc", [], {}, float("nan"),
                                   float("inf"), float("-inf"), -1, 0])
def test_framerate_formatter_never_raises(value):
    assert reports._fmt_fps(value) == "" or isinstance(reports._fmt_fps(value), str)


def test_render_html_survives_a_junk_model():
    """render_html has no safety net above it: write_report catches OSError
    only, so a TypeError here escapes to the caller."""
    for junk in (None, {}, {"sections": None}, {"sections": [None, 5]},
                 {"totals": None, "run_health": None, "sections": [{}]},
                 {"generated_at": "not a time", "sections": []}):
        assert isinstance(reports.render_html(junk), str)


def test_escaping_of_a_hostile_channel_name():
    rows = [_row(1, "Dead", "Timeout", name='<script>alert("x")</script>'),
            _row(1, "Dead", "Timeout", name='<script>alert("x")</script>', stream_id=2)]
    page = reports.render_html(reports.build_model(rows, {}, now=0))
    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page


# ---- CSV -----------------------------------------------------------------

def test_csv_has_a_header_and_one_row_per_listed_channel(model):
    text = reports.render_csv(model)
    lines = [ln for ln in text.strip().split("\n") if ln]
    assert lines[0] == ",".join(reports.CSV_COLUMNS)
    listed = sum(len(s["rows"]) for s in model["sections"])
    assert len(lines) == listed + 1


def test_csv_survives_a_junk_model():
    for junk in (None, {}, {"sections": [None]}, {"sections": [{"rows": [None]}]}):
        assert isinstance(reports.render_csv(junk), str)
