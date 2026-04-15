from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Checkbox, DataTable, Input, Static

from findmyjob.core.enums import CompanySizeBucket, ExperienceLevel, LocationScope, WorkplaceType
from findmyjob.core.types import JobSearchQuery, SavedSearch
from findmyjob.tui.common import csv_values, enum_values, int_value, join_values, tri_bool


class SearchBuilderView(VerticalScroll):
    def __init__(self) -> None:
        super().__init__(id="search-builder-view")
        self.saved_searches_table = DataTable(id="saved-searches-table")
        self.saved_search_refs: list[str] = []
        self.summary = Static(id="search-summary")
        self.name_input = Input(id="search-name")
        self.description_input = Input(id="search-description")
        self.title_keywords_input = Input(id="search-title-keywords")
        self.keyword_input = Input(id="search-keyword")
        self.source_input = Input(id="search-source")
        self.board_input = Input(id="search-board")
        self.locations_input = Input(id="search-locations")
        self.countries_input = Input(id="search-countries")
        self.regions_input = Input(id="search-regions")
        self.cities_input = Input(id="search-cities")
        self.workplace_types_input = Input(id="search-workplace-types")
        self.employment_types_input = Input(id="search-employment-types")
        self.location_scopes_input = Input(id="search-location-scopes")
        self.experience_levels_input = Input(id="search-experience-levels")
        self.company_sizes_input = Input(id="search-company-sizes")
        self.posted_within_input = Input(id="search-posted-within")
        self.compensation_present_input = Input(id="search-compensation-present")
        self.compensation_min_input = Input(id="search-compensation-min")
        self.compensation_currency_input = Input(id="search-compensation-currency")
        self.sponsorship_fit_input = Input(id="search-sponsorship-fit")
        self.limit_input = Input(value="50", id="search-limit")
        self.remote_only = Checkbox("Remote only", id="search-remote-only")
        self.active_only = Checkbox("Active only", id="search-active-only")
        self.allow_unknown_comp = Checkbox("Allow unknown compensation", id="search-allow-unknown-comp")
        self.allow_unknown_exp = Checkbox("Allow unknown experience", id="search-allow-unknown-exp")
        self.default_on_save = Checkbox("Mark as default on save", id="search-default-on-save")

    def compose(self) -> ComposeResult:
        yield Static("Search Builder", classes="screen-title")
        yield self.summary
        yield Horizontal(
            Vertical(
                Static("Saved Searches", classes="section-title"),
                self.saved_searches_table,
                Horizontal(
                    Button("Load", id="load-search"),
                    Button("Delete", id="delete-search"),
                    Button("Set Default", id="set-default-search"),
                    classes="action-row",
                ),
                classes="pane",
            ),
            Vertical(
                Static("Saved Search Metadata", classes="section-title"),
                self.name_input,
                self.description_input,
                Static("Structured Filters", classes="section-title"),
                self.title_keywords_input,
                self.keyword_input,
                self.source_input,
                self.board_input,
                self.locations_input,
                self.countries_input,
                self.regions_input,
                self.cities_input,
                self.workplace_types_input,
                self.employment_types_input,
                self.location_scopes_input,
                self.experience_levels_input,
                self.company_sizes_input,
                self.posted_within_input,
                self.compensation_present_input,
                self.compensation_min_input,
                self.compensation_currency_input,
                self.sponsorship_fit_input,
                self.limit_input,
                self.remote_only,
                self.active_only,
                self.allow_unknown_comp,
                self.allow_unknown_exp,
                self.default_on_save,
                Horizontal(
                    Button("Preview", id="preview-results"),
                    Button("Save", id="save-search"),
                    Button("Sync Greenhouse", id="sync-greenhouse"),
                    Button("Reset", id="reset-search"),
                    classes="action-row",
                ),
                classes="pane",
            ),
            classes="builder-layout",
        )

    def on_mount(self) -> None:
        self.saved_searches_table.add_columns("Name", "Default", "Source", "Updated")
        placeholders = {
            self.name_input: "Saved search name",
            self.description_input: "Description",
            self.title_keywords_input: "Title keywords, comma-separated",
            self.keyword_input: "FTS keyword",
            self.source_input: "Source adapter",
            self.board_input: "Board token",
            self.locations_input: "Free-text locations, comma-separated",
            self.countries_input: "Countries, comma-separated",
            self.regions_input: "Regions, comma-separated",
            self.cities_input: "Cities, comma-separated",
            self.workplace_types_input: "remote, hybrid, onsite",
            self.employment_types_input: "full_time, contract",
            self.location_scopes_input: "remote_us, city_specific",
            self.experience_levels_input: "entry_level, senior",
            self.company_sizes_input: "startup, midsize, enterprise",
            self.posted_within_input: "Posted within days",
            self.compensation_present_input: "any | present | missing",
            self.compensation_min_input: "Compensation minimum",
            self.compensation_currency_input: "Currency code",
            self.sponsorship_fit_input: "likely_compatible",
            self.limit_input: "Result limit",
        }
        for widget, placeholder in placeholders.items():
            widget.placeholder = placeholder

    def refresh_saved_searches(self, searches: list[SavedSearch]) -> None:
        self.saved_searches_table.clear(columns=False)
        self.saved_search_refs = []
        for item in searches:
            self.saved_search_refs.append(item.id or item.name)
            self.saved_searches_table.add_row(
                item.name,
                "yes" if item.is_default else "",
                item.source_adapter_hint or item.query.source_adapter or "",
                str(item.updated_at or ""),
            )

    def current_saved_search_reference(self) -> str | None:
        if not self.saved_search_refs:
            return None
        try:
            row = self.saved_searches_table.cursor_row
        except Exception:
            row = 0
        if row < 0 or row >= len(self.saved_search_refs):
            row = 0
        return self.saved_search_refs[row]

    def build_query(self) -> JobSearchQuery:
        return JobSearchQuery(
            title_keywords=csv_values(self.title_keywords_input.value),
            keyword=self.keyword_input.value.strip() or None,
            source_adapter=self.source_input.value.strip() or None,
            board_token=self.board_input.value.strip() or None,
            locations=csv_values(self.locations_input.value),
            countries=csv_values(self.countries_input.value),
            regions=csv_values(self.regions_input.value),
            cities=csv_values(self.cities_input.value),
            workplace_types=enum_values(WorkplaceType, self.workplace_types_input.value),
            employment_types=csv_values(self.employment_types_input.value),
            location_scopes=enum_values(LocationScope, self.location_scopes_input.value),
            experience_levels=enum_values(ExperienceLevel, self.experience_levels_input.value),
            company_size_buckets=enum_values(CompanySizeBucket, self.company_sizes_input.value),
            posted_within_days=int_value(self.posted_within_input.value),
            compensation_present=tri_bool(self.compensation_present_input.value),
            compensation_min=int_value(self.compensation_min_input.value),
            compensation_currency=self.compensation_currency_input.value.strip() or None,
            remote_only=self.remote_only.value,
            active_only=self.active_only.value,
            allow_unknown_compensation=self.allow_unknown_comp.value,
            allow_unknown_experience_level=self.allow_unknown_exp.value,
            sponsorship_fit=self.sponsorship_fit_input.value.strip() or None,
            limit=int_value(self.limit_input.value) or 50,
        )

    def load_query(self, query: JobSearchQuery, search: SavedSearch | None = None) -> None:
        self.title_keywords_input.value = ", ".join(query.title_keywords)
        self.keyword_input.value = query.keyword or ""
        self.source_input.value = query.source_adapter or ""
        self.board_input.value = query.board_token or ""
        self.locations_input.value = ", ".join(query.locations)
        self.countries_input.value = ", ".join(query.countries)
        self.regions_input.value = ", ".join(query.regions)
        self.cities_input.value = ", ".join(query.cities)
        self.workplace_types_input.value = join_values(query.workplace_types)
        self.employment_types_input.value = ", ".join(query.employment_types)
        self.location_scopes_input.value = join_values(query.location_scopes)
        self.experience_levels_input.value = join_values(query.experience_levels)
        self.company_sizes_input.value = join_values(query.company_size_buckets)
        self.posted_within_input.value = str(query.posted_within_days or "")
        if query.compensation_present is True:
            self.compensation_present_input.value = "present"
        elif query.compensation_present is False:
            self.compensation_present_input.value = "missing"
        else:
            self.compensation_present_input.value = ""
        self.compensation_min_input.value = str(query.compensation_min or "")
        self.compensation_currency_input.value = query.compensation_currency or ""
        self.sponsorship_fit_input.value = query.sponsorship_fit or ""
        self.limit_input.value = str(query.limit)
        self.remote_only.value = query.remote_only
        self.active_only.value = query.active_only
        self.allow_unknown_comp.value = query.allow_unknown_compensation
        self.allow_unknown_exp.value = query.allow_unknown_experience_level
        self.name_input.value = search.name if search else self.name_input.value
        self.description_input.value = search.description or "" if search else self.description_input.value
        self.default_on_save.value = search.is_default if search else False
        self.update_summary()

    def reset_form(self) -> None:
        self.name_input.value = ""
        self.description_input.value = ""
        self.default_on_save.value = False
        self.load_query(JobSearchQuery())

    def update_summary(self) -> None:
        self.summary.update(f"Current Filter Summary\n{self.build_query().summary()}")
