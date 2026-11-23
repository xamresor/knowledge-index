"""Tests for bin/link_http.py — the frontend→controller HTTP-edge matcher.

Focus: the call-site shapes, path normalization, and prefix-tolerant route
matching that the Next/React fix added (dotted client, ofetch/fetch wrappers,
base-URL `fetch()`, leading base-URL interpolation, omitted `api/` prefix).

Stdlib only — run with `python3 -m unittest discover tests` (no pytest needed).
"""
import unittest

from _kbtest import load_script

lh = load_script("link_http.py")


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

    def test_typescript_type_argument_before_the_call(self):
        self.assertEqual(calls("$api<Page<Item>>('v2/items')"), [("get", "v2/items")])
        self.assertEqual(calls("await apiFetch<User>('/auth/me', {method: 'PUT'})"),
                         [("put", "/auth/me")])

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


class UrlOnTheNextLine(unittest.TestCase):
    """Prettier puts a long call's URL on the next line: before 2026-11-20 those were not seen."""

    def test_wrapper_with_type_argument_and_method_below(self):
        src = ("  const data = await apiFetch<ReservationActionResponse>(\n"
               "    `/auth/reservations/${encodeURIComponent(id)}/dispute`,\n"
               "    {\n      method: 'POST',\n")
        self.assertEqual(calls(src), [("post", "/auth/reservations/${encodeURIComponent(id)}/dispute")])

    def test_dotted_client_and_plain_fetch(self):
        self.assertEqual(calls("api.patch(\n  '/v1/orders/1',\n  body)"), [("patch", "/v1/orders/1")])
        self.assertEqual(calls("fetch(\n  '/static/a.json')"), [])            # not a base-URL template
        self.assertEqual(calls("fetch(\n  `${API}/orders`)"), [("get", "${API}/orders")])


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


class LoadRoutes(unittest.TestCase):
    def test_reads_the_route_list_artisan_prints(self):
        # `php artisan route:list --json` is a JSON list, not a graph: loading it through the graph
        # loader broke every build that had a Laravel project (v0.3.1 → v0.10.0).
        import json
        import os
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump([{"method": "GET|HEAD", "uri": "api/orders",
                        "action": "App\\Http\\Controllers\\OrderController@index"}], fh)
        self.addCleanup(os.unlink, fh.name)
        self.assertEqual(lh.load_routes(fh.name),
                         {("get", "api/orders"): ("Http/Controllers/OrderController.php", "index")})


class CallSourceIsTheFunction(unittest.TestCase):
    """A frontend call is linked from its enclosing function, not from the whole file (2026-11-20):
    with the file as source, every function of an API module shared all of the module's routes."""

    def test_each_function_gets_its_own_route(self):
        import json
        import os
        import subprocess
        import sys
        import tempfile
        from _kbtest import BIN
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "web", "src")
            os.makedirs(src)
            with open(os.path.join(src, "api.ts"), "w") as f:
                f.write("export function createOrder() {\n  return apiFetch(`/orders`, { method: 'POST' });\n}\n"
                        "export function listOrders() {\n  return apiFetch(`/orders`);\n}\n")
            g = {"directed": False, "multigraph": False, "graph": {}, "links": [], "nodes": [
                {"id": "api::ctl", "label": "OrderController", "repo": "api", "type": "class",
                 "source_file": "app/Http/Controllers/OrderController.php", "source_location": "L5"},
                {"id": "api::store", "label": ".store()", "repo": "api", "type": "method",
                 "source_file": "app/Http/Controllers/OrderController.php", "source_location": "L9"},
                {"id": "api::index", "label": ".index()", "repo": "api", "type": "method",
                 "source_file": "app/Http/Controllers/OrderController.php", "source_location": "L14"},
                {"id": "web::file", "label": "api.ts", "repo": "web", "type": "file",
                 "source_file": "src/api.ts", "source_location": "L1"},
                {"id": "web::create", "label": "createOrder()", "repo": "web", "type": "symbol",
                 "source_file": "src/api.ts", "source_location": "L1"},
                {"id": "web::list", "label": "listOrders()", "repo": "web", "type": "symbol",
                 "source_file": "src/api.ts", "source_location": "L4"}]}
            gp, rp = os.path.join(d, "graph.json"), os.path.join(d, "routes.json")
            with open(gp, "w") as f:
                json.dump(g, f)
            with open(rp, "w") as f:
                json.dump([{"method": "POST", "uri": "orders", "action": "App\\Http\\Controllers\\OrderController@store"},
                           {"method": "GET|HEAD", "uri": "orders", "action": "App\\Http\\Controllers\\OrderController@index"}], f)
            p = subprocess.run([sys.executable, os.path.join(BIN, "link_http.py"), gp, rp, "api", src],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            with open(gp) as f:
                edges = {(e["source"], e["target"]) for e in json.load(f)["links"] if e.get("relation") == "http_request"}
        self.assertEqual(edges, {("web::create", "api::store"), ("web::list", "api::index")})


class MutationGaps(unittest.TestCase):
    """Cases added by the mutation pass (`make mutation`): each pins a decision that a one-line
    change of the matcher used to leave unnoticed."""

    def test_method_of_the_next_call_is_not_taken(self):
        # a call without `method:` must not read the `method: 'POST'` of the call right below it
        src = "apiFetch('/a')\napiFetch('/b', { method: 'POST' })"
        self.assertEqual(calls(src), [("get", "/a"), ("post", "/b")])

    def test_double_slash_collapses(self):
        self.assertEqual(lh.norm_path("auth//listings"), "auth/listings")

    def test_suffix_must_start_at_a_segment(self):
        # `listings` is a string suffix of `api/mylistings` but not a path suffix of it
        routes = {("get", "api/mylistings"): ("A.php", "index")}
        self.assertIsNone(lh.match_route(routes, "get", "listings"))

    def test_empty_path_matches_nothing(self):
        # a route uri that ends with a slash (an un-normalised table) must not match the empty path
        routes = {("get", "api/"): ("A.php", "index")}
        self.assertIsNone(lh.match_route(routes, "get", ""))

    def test_shortest_base_prefix_wins_whatever_the_table_order(self):
        # the longer route comes first and sorts first by name: only the length order picks `v2/items`
        routes = {("get", "api/v1/items"): ("Old.php", "index"),
                  ("get", "v2/items"): ("New.php", "index")}
        self.assertEqual(lh.match_route(routes, "get", "items"), ("New.php", "index"))

    def test_base_prefix_must_be_base_segments_only(self):
        # `api/auth/items` has a non-base segment, so it is no base-prefix match; two such routes are ambiguous
        routes = {("get", "api/auth/items"): ("A.php", "index"),
                  ("get", "admin/items"): ("B.php", "index")}
        self.assertIsNone(lh.match_route(routes, "get", "items"))

    def test_two_targets_behind_a_lost_prefix_are_no_match(self):
        routes = {("post", "api/invoices/{}/send"): ("Invoice.php", "send"),
                  ("post", "api/receipts/{}/send"): ("Receipt.php", "send")}
        self.assertIsNone(lh.match_route(routes, "post", "send"))

    def test_single_candidate_behind_a_lost_prefix_matches(self):
        routes = {("post", "api/invoices/{}/send"): ("Invoice.php", "send")}
        self.assertEqual(lh.match_route(routes, "post", "send"), ("Invoice.php", "send"))

    def test_unversioned_string_argument_is_not_a_call(self):
        self.assertEqual(calls("track('hello/world')"), [])
        self.assertEqual(calls("usePaginatedList(`v2/items`)"), [("get", "v2/items")])

    def test_unknown_verb_in_route_table_is_dropped(self):
        import json
        import os
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump([{"method": "GET|HEAD|OPTIONS|PROPFIND", "uri": "api/Orders/{id}",
                        "action": "App\\Http\\Controllers\\OrderController@show"}], fh)
        self.addCleanup(os.unlink, fh.name)
        self.assertEqual(lh.load_routes(fh.name),
                         {("get", "api/orders/{}"): ("Http/Controllers/OrderController.php", "show")})


class LinkEdgesEndToEnd(unittest.TestCase):
    """The whole `link_http.py` run: which call sites become edges."""

    def run_link(self, files, routes, nodes):
        import json
        import os
        import subprocess
        import sys
        import tempfile
        from _kbtest import BIN
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "web", "src")
            for rel, text in files.items():
                path = os.path.join(src, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    f.write(text)
            g = {"directed": False, "multigraph": False, "graph": {}, "links": [], "nodes": [
                {"id": "api::ctl", "label": "OrderController", "repo": "api", "type": "class",
                 "source_file": "app/Http/Controllers/OrderController.php", "source_location": "L5"},
                {"id": "api::index", "label": ".index()", "repo": "api", "type": "method",
                 "source_file": "app/Http/Controllers/OrderController.php", "source_location": "L9"}] + nodes}
            gp, rp = os.path.join(d, "graph.json"), os.path.join(d, "routes.json")
            with open(gp, "w") as f:
                json.dump(g, f)
            with open(rp, "w") as f:
                json.dump(routes, f)
            p = subprocess.run([sys.executable, os.path.join(BIN, "link_http.py"), gp, rp, "api", src],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            with open(gp) as f:
                return [e for e in json.load(f)["links"] if e.get("relation") == "http_request"]

    ROUTES = [{"method": "GET|HEAD", "uri": "v2/orders",
               "action": "App\\Http\\Controllers\\OrderController@index"}]

    def test_the_same_call_twice_in_one_function_is_one_edge(self):
        edges = self.run_link(
            {"api.ts": "export function load() {\n  apiFetch(`v2/orders`);\n  apiFetch(`v2/orders`);\n}\n"},
            self.ROUTES,
            [{"id": "web::file", "label": "api.ts", "repo": "web", "type": "file",
              "source_file": "src/api.ts", "source_location": "L1"},
             {"id": "web::load", "label": "load()", "repo": "web", "type": "symbol",
              "source_file": "src/api.ts", "source_location": "L1"}])
        self.assertEqual([(e["source"], e["target"], e["http_method"], e["http_uri"]) for e in edges],
                         [("web::load", "api::index", "GET", "v2/orders")])

    def test_a_php_backend_links_but_its_tests_folder_does_not(self):
        call = "<?php\nHttp::get('v2/orders');\n"
        node = lambda i, f: {"id": i, "label": ".sync()", "repo": "bo", "type": "method",
                             "source_file": f, "source_location": "L1"}
        edges = self.run_link(
            {"Sync.php": call, "tests/SyncTest.php": call},
            self.ROUTES,
            [node("bo::sync", "src/Sync.php"), node("bo::test", "src/tests/SyncTest.php")])
        self.assertEqual([(e["source"], e["target"]) for e in edges], [("bo::sync", "api::index")])
