"""Tests for bin/link_http.py — the frontend→controller HTTP-edge matcher.

Focus: the call-site shapes, path normalization, and prefix-tolerant route
matching that the Next/React fix added (dotted client, ofetch/fetch wrappers,
base-URL `fetch()`, leading base-URL interpolation, omitted `api/` prefix).

Stdlib only — run with `python3 -m unittest discover tests` (no pytest needed).
"""
import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "link_http", os.path.join(_HERE, "..", "bin", "link_http.py"))
lh = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lh)


def calls(src: str):
    """(verb, path) tuples for a source snippet (drops line numbers)."""
    return [(v, p) for _, v, p in lh.iter_calls(src.splitlines())]


class IterCalls(unittest.TestCase):
    def test_dotted_client(self):
        self.assertEqual(calls("api.post('/v1/x')"), [("post", "/v1/x")])
        self.assertEqual(calls("await api.get(`/v1/y`)"), [("get", "/v1/y")])

    def test_nuxt_ofetch_verb_in_options(self):
        self.assertEqual(calls("$api('v2/objects', {method: 'POST'})"),
                         [("post", "v2/objects")])

    def test_nuxt_ofetch_defaults_to_get(self):
        self.assertEqual(calls("$api('v2/objects')"), [("get", "v2/objects")])

    def test_apifetch_wrapper(self):
        self.assertEqual(calls("apiFetch('/auth/phone/send-otp', { method: 'POST' })"),
                         [("post", "/auth/phone/send-otp")])
        # verb defaults to GET when no method: in the window
        self.assertEqual(calls("const r = await apiFetch('/auth/listings')"),
                         [("get", "/auth/listings")])

    def test_apiupload_apidownload(self):
        self.assertEqual(calls("apiUpload('/auth/listings/1/images', fd)"),
                         [("get", "/auth/listings/1/images")])
        self.assertEqual(calls("apiDownload('/auth/gdpr/export', 'data.zip')"),
                         [("get", "/auth/gdpr/export")])

    def test_method_on_following_line_within_window(self):
        src = "await apiFetch('/auth/change-password', {\n  method: 'POST',\n  body,\n})"
        self.assertEqual(calls(src), [("post", "/auth/change-password")])

    def test_method_beyond_window_falls_back_to_get(self):
        # method: is 4 lines below (window is 3) → not associated
        src = "apiFetch('/x', {\n\n\n\n  method: 'DELETE'\n})"
        self.assertEqual(calls(src), [("get", "/x")])

    def test_bare_fetch_only_when_base_url_template(self):
        self.assertEqual(calls("fetch(`${API_BASE}/categories?language=lv`)"),
                         [("get", "${API_BASE}/categories?language=lv")])
        self.assertEqual(calls("await fetch(`${getApiBaseUrl()}/auth/x`, {method:'PUT'})"),
                         [("put", "${getApiBaseUrl()}/auth/x")])

    def test_bare_fetch_static_or_nonstring_ignored(self):
        self.assertEqual(calls("fetch('/static/logo.png')"), [])
        self.assertEqual(calls("fetch(event.request)"), [])
        self.assertEqual(calls("fetch(url, { headers })"), [])

    def test_lookbehind_rejects_method_call_and_prefixed_ident(self):
        # `.fetch(` is a method on something else; `myfetch(` is a different fn
        self.assertEqual(calls("caches.fetch(`${API_BASE}/x`)"), [])
        self.assertEqual(calls("myfetch(`${API_BASE}/x`)"), [])


class NormPath(unittest.TestCase):
    def test_strip_leading_base_url_interpolation(self):
        self.assertEqual(lh.norm_path("${API_BASE}/categories"), "categories")
        self.assertEqual(lh.norm_path("${getApiBaseUrl()}/auth/x"), "auth/x")

    def test_query_and_slashes_stripped_lowercased(self):
        self.assertEqual(lh.norm_path("/Auth/Listings/?language=lv"), "auth/listings")

    def test_path_params_collapse_to_placeholder(self):
        self.assertEqual(lh.norm_path("/auth/listings/${encodeURIComponent(id)}"),
                         "auth/listings/{}")
        self.assertEqual(lh.norm_path("auth/listings/{listing}/boost"),
                         "auth/listings/{}/boost")
        self.assertEqual(lh.norm_path("users/{id?}"), "users/{}")
        self.assertEqual(lh.norm_path("users/:id/posts"), "users/{}/posts")

    def test_only_leading_interpolation_stripped_not_mid_path(self):
        # a ${..} in the middle must still become {}, not vanish
        self.assertEqual(lh.norm_path("${BASE}/a/${x}/b"), "a/{}/b")


class ActionParts(unittest.TestCase):
    def test_controller_with_method(self):
        self.assertEqual(
            lh.action_parts(r"App\Http\Controllers\Api\AuthController@changePassword"),
            ("Http/Controllers/Api/AuthController.php", "changePassword"))

    def test_controller_without_method(self):
        suf, method = lh.action_parts(r"App\Http\Controllers\CategoryController")
        self.assertEqual(suf, "Http/Controllers/CategoryController.php")
        self.assertIsNone(method)

    def test_non_controller_skipped(self):
        self.assertIsNone(lh.action_parts("Closure"))
        self.assertIsNone(lh.action_parts(r"App\Livewire\SomeComponent@render"))


class MatchRoute(unittest.TestCase):
    def setUp(self):
        self.routes = {
            ("post", "api/auth/change-password"): ("AuthController.php", "changePassword"),
            ("get", "api/categories"): ("CategoryController.php", "index"),
            ("get", "api/listings"): ("PublicListingController.php", "index"),
            ("get", "api/auth/listings"): ("ListingController.php", "index"),
            ("get", "api/listings/{}"): ("PublicListingController.php", "show"),
        }

    def test_exact_match(self):
        self.assertEqual(lh.match_route(self.routes, "get", "api/categories"),
                         ("CategoryController.php", "index"))

    def test_prefix_tolerant_omitted_api(self):
        # frontend drops the `api/` the base URL supplies
        self.assertEqual(lh.match_route(self.routes, "post", "auth/change-password"),
                         ("AuthController.php", "changePassword"))

    def test_shortest_suffix_wins(self):
        # `/listings` is a suffix of both api/listings and api/auth/listings → pick shortest
        self.assertEqual(lh.match_route(self.routes, "get", "listings"),
                         ("PublicListingController.php", "index"))

    def test_verb_specific(self):
        # right path, wrong verb → no match
        self.assertIsNone(lh.match_route(self.routes, "delete", "auth/change-password"))

    def test_param_path_suffix(self):
        self.assertEqual(lh.match_route(self.routes, "get", "listings/{}"),
                         ("PublicListingController.php", "show"))

    def test_empty_and_unknown(self):
        self.assertIsNone(lh.match_route(self.routes, "get", ""))
        self.assertIsNone(lh.match_route(self.routes, "get", "nope/nowhere"))


class EndToEndCallToRoute(unittest.TestCase):
    """A frontend snippet resolves through iter_calls → norm_path → match_route
    to the right controller — the whole path the fix restored."""
    routes = {
        ("post", "api/auth/phone/send-otp"): ("PhoneVerificationController.php", "sendOtp"),
        ("get", "api/seo/landing-combos"): ("SeoController.php", "landingCombos"),
        ("get", "api/listings/{}"): ("PublicListingController.php", "show"),
    }

    def resolve(self, src):
        out = []
        for _, verb, raw in lh.iter_calls(src.splitlines()):
            out.append(lh.match_route(self.routes, verb, lh.norm_path(raw)))
        return out

    def test_apifetch_post(self):
        self.assertEqual(self.resolve("apiFetch('/auth/phone/send-otp', {method:'POST'})"),
                         [("PhoneVerificationController.php", "sendOtp")])

    def test_base_url_fetch_get(self):
        self.assertEqual(self.resolve("fetch(`${API_BASE}/seo/landing-combos`, {next:{revalidate:3600}})"),
                         [("SeoController.php", "landingCombos")])

    def test_base_url_fetch_with_param(self):
        self.assertEqual(
            self.resolve("fetch(`${API_BASE}/listings/${encodeURIComponent(slug)}`)"),
            [("PublicListingController.php", "show")])


if __name__ == "__main__":
    unittest.main()
