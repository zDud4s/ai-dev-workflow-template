# Feature: command-line argument parser

Implement `parseArgs(argv, spec)` in `src/parser.js` (CommonJS export
`{ parseArgs }`). It parses an array of CLI tokens against a spec and returns
`{ options, positionals }`.

`argv` is an array of strings (like `process.argv.slice(2)`).

`spec` maps an option name to a descriptor:
- `type`: `"boolean"`, `"string"`, or `"number"` (required)
- `alias`: optional single-character short name
- `default`: optional default value when the option is absent
- `required`: optional boolean

Parsing rules:
- Long options: `--name value` or `--name=value` for string/number options.
- Boolean options: `--name` sets `true`; `--no-name` sets `false`. Booleans never
  consume a following token.
- Short aliases: `-a` resolves to the option whose `alias` is `a`. A short
  string/number option takes the next token as its value (`-p 8080`).
- `--` terminates option parsing; every token after it is a positional.
- Any token that is not an option (and not consumed as a value) is a positional,
  collected in order into `positionals`.
- `number` options coerce their value with `Number(...)`; if the result is `NaN`,
  throw an `Error`.
- An unknown long or short option throws an `Error`.
- A `required` option that is never provided throws an `Error`.
- `default` values are applied to options that were not provided.

Return shape: `{ options: { <name>: <value>, ... }, positionals: [ ... ] }`.
Only include in `options` the names that were provided or have a default.
