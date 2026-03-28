import os
import streamlit as st

# Inject Streamlit Cloud secrets into env vars before utils is imported.
# On Streamlit Cloud: st.secrets holds the credentials set in the dashboard.
# Locally: this silently does nothing and load_dotenv() in utils.py handles it.
try:
    os.environ.update({k: str(v) for k, v in st.secrets.items() if isinstance(v, str)})
except Exception:
    pass

import pandas as pd
from datetime import datetime, timedelta
from google.oauth2.credentials import Credentials
from utils import (
    get_reviews,
    parse_reviews_to_lists,
    analyze_review_and_suggest_response,
    generate_analytics_dashboard,
    get_google_login_url,
    complete_google_login,
    list_gmb_locations,
)

st.set_page_config(
    page_title="Panel Opinii",
    page_icon="🏨",
    layout="wide"
)

REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:8501")

# ── Google OAuth login gate ───────────────────────────────────────────────────
if "user_email" not in st.session_state:
    code = st.query_params.get("code")
    if code:
        try:
            info = complete_google_login(code, REDIRECT_URI)
            st.session_state.user_email = info["email"]
            st.session_state.user_name = info["name"]
            st.session_state.google_token = info["token"]
            st.session_state.google_refresh_token = info["refresh_token"]
            st.session_state.google_token_uri = info["token_uri"]
            st.session_state.google_scopes = info["scopes"]
            st.query_params.clear()
            st.rerun()
        except Exception as e:
            st.query_params.clear()
            st.error(f"Błąd logowania: {e}")
            st.stop()
    else:
        _, col, _ = st.columns([1, 1, 1])
        with col:
            st.title("🏨 Panel Opinii")
            login_url = get_google_login_url(REDIRECT_URI)
            st.link_button("Zaloguj się przez Google", login_url, use_container_width=True, type="primary")
        st.stop()

STAR_MAP = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}


# ── Credential helper ─────────────────────────────────────────────────────────
def get_creds() -> Credentials:
    return Credentials(
        token=st.session_state.google_token,
        refresh_token=st.session_state.google_refresh_token,
        token_uri=st.session_state.google_token_uri,
        client_id=os.getenv("GOOGLE_API_OAUTH_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_API_OAUTH_CLIENT_SECRET"),
        scopes=st.session_state.google_scopes,
    )


# ── Discover GMB locations once per session ───────────────────────────────────
if "gmb_locations" not in st.session_state:
    with st.spinner("Łączę z Google Business Profile..."):
        st.session_state.gmb_locations = list_gmb_locations(get_creds())

if not st.session_state.gmb_locations:
    st.error("Nie znaleziono żadnych lokalizacji w Google Business Profile dla tego konta.")
    st.stop()

if "location_id" not in st.session_state:
    st.session_state.location_id = st.session_state.gmb_locations[0]["location_id"]

if "location_context" not in st.session_state:
    st.session_state.location_context = {}


def get_current_location() -> dict:
    return next(
        (loc for loc in st.session_state.gmb_locations if loc["location_id"] == st.session_state.location_id),
        st.session_state.gmb_locations[0],
    )


# ── Session State ─────────────────────────────────────────────────────────────
def _reset_reviews():
    st.session_state.reviews = []
    st.session_state.unanswered = []
    st.session_state.answered = []
    st.session_state.examples = []
    st.session_state.analytics_loaded = False
    st.session_state.analytics_data = {}

if "reviews" not in st.session_state:
    _reset_reviews()
if "analytics_loaded" not in st.session_state:
    st.session_state.analytics_loaded = False
if "analytics_data" not in st.session_state:
    st.session_state.analytics_data = {}


# ── Helpers ───────────────────────────────────────────────────────────────────
def stars(rating: str) -> str:
    return "⭐" * STAR_MAP.get(rating, 0)


def review_uid(r: dict) -> str:
    return f"{r['reviewer']}_{r['date']}_{r['rating']}"


def all_parsed() -> list:
    return st.session_state.unanswered + st.session_state.answered


def current_context() -> str:
    return st.session_state.location_context.get(st.session_state.location_id, "")


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("🏨 Panel Opinii")
    st.caption(f"**{st.session_state.user_name}**")
    st.caption(st.session_state.user_email)
    if st.button("Wyloguj", use_container_width=True):
        for key in ["user_email", "user_name", "google_token", "google_refresh_token",
                    "google_token_uri", "google_scopes", "gmb_locations"]:
            st.session_state.pop(key, None)
        _reset_reviews()
        st.rerun()

    st.divider()

    # Location selector — only shown if user manages more than one property
    if len(st.session_state.gmb_locations) > 1:
        location_names = [loc["name"] for loc in st.session_state.gmb_locations]
        current_idx = next(
            (i for i, loc in enumerate(st.session_state.gmb_locations)
             if loc["location_id"] == st.session_state.location_id), 0
        )
        selected_name = st.selectbox("Obiekt", location_names, index=current_idx)
        selected_loc = next(loc for loc in st.session_state.gmb_locations if loc["name"] == selected_name)
        if selected_loc["location_id"] != st.session_state.location_id:
            st.session_state.location_id = selected_loc["location_id"]
            _reset_reviews()
            st.rerun()
    else:
        st.markdown(f"**{st.session_state.gmb_locations[0]['name']}**")

    # Optional property description for AI prompts
    with st.expander("Opis obiektu (dla AI)"):
        ctx = st.text_area(
            "Opisz obiekt — lokalizacja, udogodnienia, styl. Im więcej szczegółów, tym lepsze odpowiedzi AI.",
            value=current_context(),
            height=140,
            label_visibility="collapsed",
        )
        st.session_state.location_context[st.session_state.location_id] = ctx

    st.divider()

    if st.button("🔄 Pobierz opinie z Google", use_container_width=True, type="primary"):
        loc = get_current_location()
        with st.spinner("Pobieram opinie z Google API..."):
            raw = get_reviews(get_creds(), loc["account_id"], loc["location_id"])
            st.session_state.reviews = raw
            u, a, e = parse_reviews_to_lists(raw)
            st.session_state.unanswered = u
            st.session_state.answered = a
            st.session_state.examples = e
            st.session_state.analytics_loaded = False
        st.success(f"Pobrano {len(raw)} opinii.")

    if st.session_state.reviews:
        total = len(all_parsed())
        answered_n = len(st.session_state.answered)
        unanswered_n = len(st.session_state.unanswered)
        avg_r = sum(STAR_MAP.get(r["rating"], 0) for r in all_parsed()) / total if total else 0
        resp_rate = (answered_n / total * 100) if total else 0

        st.divider()
        st.metric("Łącznie opinii", total)
        st.metric("Bez odpowiedzi", unanswered_n)
        st.metric("Odpowiedziano", f"{resp_rate:.0f}%")
        st.metric("Średnia ocena", f"{avg_r:.2f} / 5.0")


# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_overview, tab_new, tab_history, tab_analytics = st.tabs([
    "📊 Przegląd",
    "⭐ Nowe Opinie",
    "📚 Historia Opinii",
    "📈 Analityka AI",
])


# ════════════════════════════════════════════════════════════════════════════
# TAB 1 · OVERVIEW
# ════════════════════════════════════════════════════════════════════════════
with tab_overview:
    if not st.session_state.reviews:
        st.info("Pobierz opinie z Google API (panel boczny), aby zobaczyć dashboard.")
    else:
        parsed = all_parsed()
        total = len(parsed)
        answered_n = len(st.session_state.answered)
        unanswered_n = len(st.session_state.unanswered)
        avg_r = sum(STAR_MAP.get(r["rating"], 0) for r in parsed) / total if total else 0
        resp_rate = (answered_n / total * 100) if total else 0

        cutoff_7d = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        cutoff_14d = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
        this_week = [r for r in parsed if r["date"] >= cutoff_7d]
        prev_week = [r for r in parsed if cutoff_14d <= r["date"] < cutoff_7d]

        st.subheader("Kluczowe Wskaźniki")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Łącznie opinii", total)
        c2.metric("Średnia ocena", f"{avg_r:.2f} / 5.0")
        c3.metric("Wskaźnik odpowiedzi", f"{resp_rate:.0f}%")
        c4.metric("Nowe w tym tygodniu", len(this_week), delta=len(this_week) - len(prev_week))

        st.divider()

        chart_col1, chart_col2 = st.columns(2)

        with chart_col1:
            st.subheader("Rozkład ocen")
            rating_counts = {k: 0 for k in range(1, 6)}
            for r in parsed:
                n = STAR_MAP.get(r["rating"], 0)
                if n:
                    rating_counts[n] += 1
            df_ratings = pd.DataFrame(
                {"Liczba opinii": rating_counts.values()},
                index=[f"{'⭐' * k} ({k})" for k in rating_counts.keys()],
            )
            st.bar_chart(df_ratings)

        with chart_col2:
            st.subheader("Opinie w czasie (miesięcznie)")
            monthly: dict[str, int] = {}
            for r in parsed:
                month = r["date"][:7]
                monthly[month] = monthly.get(month, 0) + 1
            if monthly:
                sorted_months = sorted(monthly.keys())
                df_monthly = pd.DataFrame(
                    {"Opinie": [monthly[m] for m in sorted_months]},
                    index=sorted_months,
                )
                st.line_chart(df_monthly)

        st.divider()

        st.subheader("📅 Raport Tygodniowy")
        w1, w2, w3 = st.columns(3)
        w1.metric("Opinie (ten tydzień)", len(this_week), delta=len(this_week) - len(prev_week))

        if this_week:
            week_avg = sum(STAR_MAP.get(r["rating"], 0) for r in this_week) / len(this_week)
            prev_avg = (
                sum(STAR_MAP.get(r["rating"], 0) for r in prev_week) / len(prev_week)
                if prev_week else None
            )
            w2.metric(
                "Śr. ocena (ten tydzień)",
                f"{week_avg:.2f}",
                delta=f"{week_avg - prev_avg:+.2f}" if prev_avg else None,
            )
            unanswered_week = sum(1 for r in this_week if not r.get("our_response"))
            w3.metric("Bez odpowiedzi (ten tydzień)", unanswered_week)

            with st.expander(f"Zobacz {len(this_week)} opinii z ostatnich 7 dni"):
                for r in sorted(this_week, key=lambda x: x["date"], reverse=True):
                    answered_badge = "✅ Odpowiedziano" if r.get("our_response") else "⚠️ Brak odpowiedzi"
                    st.markdown(f"**{r['reviewer']}** ({r['date']}) {stars(r['rating'])}  ·  {answered_badge}")
                    if r.get("comment"):
                        preview = r["comment"][:200]
                        st.markdown(f"> {preview}{'…' if len(r['comment']) > 200 else ''}")
                    st.divider()
        else:
            w2.metric("Śr. ocena", "—")
            w3.metric("Bez odpowiedzi", "—")
            st.info("Brak nowych opinii w ostatnich 7 dniach.")


# ════════════════════════════════════════════════════════════════════════════
# TAB 2 · NEW REVIEWS
# ════════════════════════════════════════════════════════════════════════════
with tab_new:
    st.header("Nowe Opinie — Wymagające Odpowiedzi")

    if not st.session_state.reviews:
        st.info("Brak opinii w pamięci. Kliknij 'Pobierz opinie' w panelu bocznym.")
    elif not st.session_state.unanswered:
        st.success("Wszystkie opinie mają odpowiedź! 🎉")
    else:
        ctrl1, ctrl2, ctrl3 = st.columns([2, 2, 3])
        with ctrl1:
            sort_by = st.selectbox(
                "Sortuj według",
                ["Najnowsze", "Najstarsze", "Najniższa ocena", "Najwyższa ocena"],
            )
        with ctrl2:
            filter_stars_sel = st.multiselect(
                "Pokaż tylko oceny",
                [1, 2, 3, 4, 5],
                format_func=lambda x: "⭐" * x,
            )
        with ctrl3:
            analyze_all = st.button(
                "🤖 Analizuj wszystkie naraz",
                use_container_width=True,
                help="Wygeneruje sugestie odpowiedzi dla wszystkich nieobsłużonych opinii.",
            )

        if analyze_all:
            progress_bar = st.progress(0, text="Analizuję opinie...")
            total_u = len(st.session_state.unanswered)
            for idx, r in enumerate(st.session_state.unanswered):
                uid = review_uid(r)
                gen_key = f"gen_{uid}"
                if gen_key not in st.session_state:
                    analysis = analyze_review_and_suggest_response(
                        r["comment"], r["rating"], r["reviewer"],
                        st.session_state.examples, current_context(),
                    )
                    st.session_state[gen_key] = analysis
                progress_bar.progress(
                    (idx + 1) / total_u,
                    text=f"Analizuję opinię {idx + 1} / {total_u}…",
                )
            st.rerun()

        indexed = list(enumerate(st.session_state.unanswered))

        if sort_by == "Najnowsze":
            indexed.sort(key=lambda x: x[1]["date"], reverse=True)
        elif sort_by == "Najstarsze":
            indexed.sort(key=lambda x: x[1]["date"])
        elif sort_by == "Najniższa ocena":
            indexed.sort(key=lambda x: STAR_MAP.get(x[1]["rating"], 0))
        elif sort_by == "Najwyższa ocena":
            indexed.sort(key=lambda x: STAR_MAP.get(x[1]["rating"], 0), reverse=True)

        if filter_stars_sel:
            indexed = [(i, r) for i, r in indexed if STAR_MAP.get(r["rating"], 0) in filter_stars_sel]

        st.caption(f"Wyświetlam **{len(indexed)}** z **{len(st.session_state.unanswered)}** nieobsłużonych opinii.")

        for orig_idx, r in indexed:
            uid = review_uid(r)
            gen_key = f"gen_{uid}"
            rating_n = STAR_MAP.get(r["rating"], 0)

            with st.container(border=True):
                header_col, badge_col = st.columns([5, 1])
                with header_col:
                    st.markdown(f"### {r['reviewer']}  ·  {stars(r['rating'])}  ·  {r['date']}")
                with badge_col:
                    if rating_n <= 2:
                        st.error("Krytyczna")
                    elif rating_n == 3:
                        st.warning("Neutralna")
                    else:
                        st.success("Pozytywna")

                if r.get("comment"):
                    st.markdown(f"> *{r['comment']}*")
                else:
                    st.caption("*(Brak opisu — tylko ocena gwiazdkowa)*")

                if gen_key not in st.session_state:
                    if st.button("🤖 Wygeneruj sugestię odpowiedzi", key=f"btn_{uid}"):
                        with st.spinner("Analiza AI w toku…"):
                            analysis = analyze_review_and_suggest_response(
                                r["comment"], r["rating"], r["reviewer"],
                                st.session_state.examples, current_context(),
                            )
                            st.session_state[gen_key] = analysis
                        st.rerun()
                else:
                    ans = st.session_state[gen_key]

                    pts_col, neg_col = st.columns(2)
                    with pts_col:
                        st.success("**✅ Plusy gościa:**")
                        for pt in ans.get("good_points", "Brak").split("\n"):
                            if pt.strip():
                                st.write(f"- {pt.strip()}")
                    with neg_col:
                        st.error("**❌ Minusy / uwagi:**")
                        for pt in ans.get("bad_points", "Brak").split("\n"):
                            if pt.strip():
                                st.write(f"- {pt.strip()}")

                    st.markdown("---")
                    st.markdown("**📝 Sugerowana odpowiedź** *(edytuj, skopiuj i wklej ręcznie do Google)*")

                    draft = ans.get("suggested_response", "")
                    edited = st.text_area(
                        "Treść odpowiedzi",
                        value=draft,
                        height=160,
                        key=f"draft_{uid}",
                        label_visibility="collapsed",
                    )
                    char_n = len(edited)
                    if char_n > 3500:
                        st.warning(f"⚠️ Odpowiedź jest długa ({char_n} znaków). Google zaleca max ~4000.")
                    else:
                        st.caption(f"{char_n} / 4000 znaków")

                    if st.button("🔄 Wygeneruj ponownie", key=f"regen_{uid}", type="secondary"):
                        del st.session_state[gen_key]
                        st.rerun()


# ════════════════════════════════════════════════════════════════════════════
# TAB 3 · HISTORY
# ════════════════════════════════════════════════════════════════════════════
with tab_history:
    st.header("Historia Wszystkich Opinii")

    if not st.session_state.reviews:
        st.info("Pobierz opinie z Google API, aby zobaczyć historię.")
    else:
        parsed = all_parsed()

        f1, f2, f3 = st.columns([2, 2, 3])
        with f1:
            filter_s = st.multiselect(
                "Filtruj po ocenie",
                [1, 2, 3, 4, 5],
                format_func=lambda x: "⭐" * x,
                key="hist_stars",
            )
        with f2:
            filter_status = st.radio(
                "Status odpowiedzi",
                ["Wszystkie", "Z odpowiedzią", "Bez odpowiedzi"],
                horizontal=True,
                key="hist_status",
            )
        with f3:
            search_q = st.text_input(
                "Szukaj w treści opinii",
                placeholder="np. śniadanie, pokój, parking…",
                key="hist_search",
            )

        filtered = parsed
        if filter_s:
            filtered = [r for r in filtered if STAR_MAP.get(r["rating"], 0) in filter_s]
        if filter_status == "Z odpowiedzią":
            filtered = [r for r in filtered if r.get("our_response")]
        elif filter_status == "Bez odpowiedzi":
            filtered = [r for r in filtered if not r.get("our_response")]
        if search_q:
            q = search_q.lower()
            filtered = [r for r in filtered if q in (r.get("comment") or "").lower()]

        filtered.sort(key=lambda x: x["date"], reverse=True)

        st.caption(f"Wyświetlam **{len(filtered)}** z **{len(parsed)}** opinii")

        view = st.radio("Widok", ["Tabela", "Karty"], horizontal=True, key="hist_view")

        if view == "Tabela":
            rows = [
                {
                    "Data": r["date"],
                    "Gość": r["reviewer"],
                    "Ocena": "⭐" * STAR_MAP.get(r["rating"], 0),
                    "Opinia": (r.get("comment") or "")[:120] + ("…" if len(r.get("comment") or "") > 120 else ""),
                    "Odpowiedź": "✅" if r.get("our_response") else "❌",
                }
                for r in filtered
            ]
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            limit = 50
            for r in filtered[:limit]:
                answered_badge = "✅" if r.get("our_response") else "⚠️"
                label = f"{answered_badge}  {stars(r['rating'])}  **{r['reviewer']}**  —  {r['date']}"
                with st.expander(label):
                    st.markdown(f"*{r.get('comment') or '(Brak opisu)'}*")
                    if r.get("our_response"):
                        st.divider()
                        st.markdown("**Odpowiedź właściciela:**")
                        st.markdown(r["our_response"])
            if len(filtered) > limit:
                st.info(f"Pokazano {limit} z {len(filtered)} wyników. Użyj filtrów, aby zawęzić listę.")


# ════════════════════════════════════════════════════════════════════════════
# TAB 4 · AI ANALYTICS
# ════════════════════════════════════════════════════════════════════════════
with tab_analytics:
    st.header("Analityka AI — Trendy i Wzorce")
    st.markdown(
        "Analiza wszystkich **odpowiedzianych** opinii w poszukiwaniu powtarzających się wzorców, "
        "mocnych stron obiektu i obszarów wymagających poprawy."
    )

    if not st.session_state.reviews:
        st.info("Najpierw pobierz opinie w panelu bocznym.")
    elif len(st.session_state.answered) < 5:
        st.warning(f"Za mało danych ({len(st.session_state.answered)} opinii). Potrzebujemy co najmniej 5 rozwiązanych.")
    else:
        st.metric("Opinie analizowane przez AI", len(st.session_state.answered))

        if not st.session_state.analytics_loaded:
            if st.button("📊 Generuj Analitykę AI", use_container_width=True, type="primary"):
                with st.spinner("Przeszukuję wszystkie opinie, szukam trendów…"):
                    analytics = generate_analytics_dashboard(st.session_state.answered, current_context())
                    st.session_state.analytics_data = analytics
                    st.session_state.analytics_loaded = True
                st.rerun()
        else:
            ans = st.session_state.analytics_data

            st.subheader("📝 Podsumowanie")
            st.info(ans.get("executive_summary", ""))

            st.divider()

            col1, col2 = st.columns(2)
            with col1:
                st.success("**🏆 Najbardziej chwalone elementy**")
                for praise in ans.get("top_praises", []):
                    st.markdown(f"- ✅ {praise}")
            with col2:
                st.error("**⚠️ Główne obszary do poprawy**")
                for issue in ans.get("areas_to_improve", []):
                    st.markdown(f"- ❌ {issue}")

            st.divider()

            if st.button("🔄 Wygeneruj raport ponownie", key="regen_analytics"):
                st.session_state.analytics_loaded = False
                st.rerun()
