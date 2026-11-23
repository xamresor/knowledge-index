# Data dictionary

The graph knows where the code uses a table. It does not know what the table means. The
`datadict` add-on adds that meaning from a small file that a person writes for each project. Think
of it as a label on each box in a store room: the code graph says where the box is, the label says
what is inside.

With the add-on on, the KB can answer "which table holds revenue per customer", not only "where is
the `orders` table in the code".

## Switch it on

```sh
kb addon install datadict      # or KB_ADDONS=...,datadict
```

Restart the KB server after that. With the add-on off, `search`, `graph_explain` and `problems` work
as before.

## Write the file

Put one file per project in the `datadict/` folder of the KB config (in a checkout: the checkout
root; installed: `~/.config/knowledge-index/datadict/`). `KB_DATADICT_DIR` points to another folder.
Name the file after the project in `kb.projects.toml`: `datadict/shop-api.toml` for `shop-api`.
The folder is git-ignored, like `aliases.toml`: it names real tables.

```sh
mkdir -p datadict && cp datadict.toml.example datadict/shop-api.toml
```

Write one `[[table]]` entry per table. Only `name` is required.

| Field | What to write |
|---|---|
| `name` | The table name as the code writes it (`$table`, `Schema::create`, `DB::table`). |
| `term` | What the business calls it: "Customer order". |
| `grain` | What one row stands for: "one row per order a customer placed". |
| `summable` | Columns that may be added up: amounts, quantities. |
| `not_summable` | Columns that must never be added up: IDs, foreign keys, unit prices, rates, statuses, running balances. |
| `synonyms` | Other words people use for this data: "sale", "revenue". |
| `notes` | A trap a reader must know before using the numbers. |

```toml
[[table]]
name = "orders"
term = "Customer order"
grain = "one row per order a customer placed"
summable = ["total_amount", "tax_amount"]
not_summable = ["id", "customer_id", "status"]
synonyms = ["sale", "purchase", "revenue"]
notes = "Cancelled orders stay in the table: filter status != 'cancelled' before you sum revenue."
```

The KB reads the file on every call. An edit needs no rebuild.

Write the words people really type into `term` and `synonyms`. Search matches them as whole words.
The grain, the column names and the notes count less.

## What changes in the tools

- **`search`** (the code half): a "Data dictionary" block comes first. It lists up to 5 tables whose
  term, synonyms or name match the question, with the grain and the columns to sum.
- **`graph_explain`** of a table: the meaning shows under the node header. A business term that is
  not a node name ("revenue") is read as the table it names. A real node name always wins.
- **`problems`**: three new kinds, for projects that have a file.
  - `datadict-invalid`: a mistake in the file (bad TOML, wrong type, unknown key, a file name that is
    not a project).
  - `datadict-unknown-table`: an entry for a table the graph does not know. Look for a typo or a
    dropped table. Tables that only `.sql` files create are not in the graph.
  - `datadict-missing`: a table that the project's code uses has no entry.

`bin/datadict.py check` prints the same problems from the command line.

A caller of the shared server who may see only some projects sees only the files of those projects.

## Check that it helps: 20 questions

Keep the add-on only if it makes answers about data better. Measure it.

1. Copy the example: `cp bench/datadict-questions.example.json bench/local/datadict-questions.json`.
   The `bench/local/` folder is git-ignored.
2. Replace the demo questions with about 20 real questions about your data. Give each one the
   tables that answer it in `expected_tables`.
3. Write the dictionary files for those projects.
4. Run the check on the machine with the built graph:

   ```sh
   python3 bench/datadict_eval.py                    # search domain "both", as agents call it
   python3 bench/datadict_eval.py --domain code      # code half only, no qmd needed
   python3 bench/datadict_eval.py --json out.json    # also save every answer's tables
   ```

The script asks `search` each question twice: with the add-on off and on. From each answer it takes
the first 5 distinct table names. recall@5 is the share of expected tables among them. The last line
prints recall@5 for both runs and how many questions got better or worse.

A table named like a common word (`users`) also counts when a doc only says the word. Both runs
have this bias, so compare the two numbers, not each one with 1.0.

Do not write the questions from the dictionary. Questions that reuse the words of the file measure
the file, not the help to a real reader.
