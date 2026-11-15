# Persona pattern

Adapted from Appendix B of the "Persistent Bot" design doc, which documents
the pattern in `dougbunger/candidsail` (`config.py`, `router.py`,
`router_drivers/`). Persist Bot keeps the configuration compatible and ports
only the pattern: no briefs, agendas, sessions or PID locks.

The CandidSail source is not required to build this. If it is available in
a session, treat it as reference, and treat this file as the contract.

## Three lookups, two keys

- The **agent name** is a label (`Raven`). It appears in the prompt and logs.
- The **persona** is the key into `conf/persona*.json`. It is a reusable
  recipe for reaching one model.
- The persona's **provider**, lowercased, is the key into `conf/provider.json`
  and into the keys file.

Nothing else connects them. Two agents may share a persona.

## Files

`conf/persona*.json`: `persona.json` loads first, then the others in sorted
filename order. A file is a flat map of name to definition, or that map
under a top-level `"persona"` key.

| Field | Required | Purpose |
|---|---|---|
| `model` | yes | Model ID exactly as the provider expects it |
| `provider` | yes | Who hosts it; lowercased for lookups |
| `driver` | no | Overrides the provider's driver |
| `url` | no | Overrides the provider's base URL |
| `vendor` | no | Informational |
| `character` | no | Text appended to the system prompt |
| `parameters` | no | Model parameters; override the provider's |
| `price` | no | Informational, input/output per million tokens |

`conf/provider.json`: keyed by lowercase provider name. This file is new in
Persist Bot; CandidSail carries `driver` and `url` on the persona.

```json
{
  "openrouter": {
    "driver": "openai",
    "url": "https://openrouter.ai/api/v1",
    "parameters": { "temperature": 0.7 }
  }
}
```

`conf/keys.json`: keyed by lowercase provider name. The entry shape belongs
to the driver; the `openai` driver reads `key`.

```json
{
  "openrouter": { "key": "" },
  "bichon":     { "key": "" }
}
```

None of the three live files is committed. The repo carries examples in
`conf/examples/` (`persona.json`, `provider.json`, `keys.json`) for the
operator to copy into `conf/`. The examples are in their own directory so
the `conf/persona*.json` discovery never loads one as live config.

## Resolution

Done once at startup, producing a self-contained `ResolvedModel`. Nothing
downstream reads the files again.

`resolve(name, persona, character=None, parameters=None)`:

1. Look up the persona. Missing: `PersonaNotFoundError`.
2. Lowercase `provider`. Missing from `provider.json`:
   `ConfigurationError`. Missing from the keys file: `AuthenticationError`.
3. `driver` and `url`: the persona's value if set, else the provider's.
   No driver from either: `ConfigurationError`.
4. Parameters: provider, then persona, then caller. A caller value of
   `None` is dropped so it does not erase a default.
5. Character: the caller's if non-empty, else the persona's.

| `ResolvedModel` field | Source |
|---|---|
| `name` | Caller |
| `model`, `vendor` | Persona |
| `provider` | Persona, lowercased |
| `driver`, `url` | Persona, else provider |
| `character` | Caller, else persona |
| `parameters` | Provider, persona, caller, in that order |
| `auth` | The whole keys entry for the provider |

At call time the driver map is indexed by `driver`, and the driver function
takes the `ResolvedModel`. Unknown driver: `ValueError`, raised at
resolution rather than at the first call.

The identity line added to the system prompt is
`Your name for this session is {name}.` followed by the character text.

## Invariants

- Secrets never appear in persona or provider files.
- Name is not persona.
- Every failure above happens at startup, before any model call.
- `character`, `vendor`, `model`, `driver`, `provider`, `url` and `price`
  inside `parameters` are a `ConfigurationError`.

## Differences from CandidSail, on purpose

Appendix B lists soft spots and asks for a decision before tests are
written. These are the decisions:

| CandidSail behaviour | Persist Bot |
|---|---|
| Empty `provider` skips the key check and fails on first call | `provider` is required |
| Duplicate persona name warns, later file wins | Error naming both files |
| Agent names are unvalidated | Must match `[A-Za-z0-9_-]+` |
| Parameter values are strings cast per driver | Native JSON numbers; a string is a `ConfigurationError` |
| Keyless self-hosted provider still needs a dummy key | Entry must exist; an empty `key` is accepted and a placeholder is sent |
| Persona files are committed; `keys_example.json` is the only template | No live persona, provider or keys file is committed; examples for all three are in `conf/examples/` |
| Six drivers | `openai` only |
| `temperature`, `timeout` and `reasoning` are first-class parameters, the rest ride in `extra` | `timeout` goes to the client; every other parameter is passed to the request as given |
| `ResolvedModel.routing` carries client and referrer headers from global config | Not ported |
| Renaming a persona silently breaks the callers that use the old name | A test resolves every persona named in the shipped `conf/init.yaml` against the shipped persona files |

## Required tests

Unknown persona, missing provider entry, missing keys entry, parameter
override order across all three layers, character fallback, duplicate name
across two files, persona-level field inside `parameters`, unknown driver,
and the shipped `conf/init.yaml` resolving against the files in
`conf/examples/`.
