Create solution.py exposing `json_pointer_get(doc, pointer)` that resolves a JSON Pointer (RFC 6901) string against doc (made of nested dicts and lists) and returns the referenced value.

Rules:
- The empty string "" returns doc itself.
- Otherwise the pointer is a sequence of tokens, each prefixed by "/".
- Within a token, the escape "~1" decodes to "/" and "~0" decodes to "~".
- A token addressing a list is the decimal index into that list.
