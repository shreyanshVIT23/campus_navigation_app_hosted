# types-lxml (dev dependency)

- **Package:** `types-lxml`, in the `dev` dependency group
- **Why:** `lxml.etree` is a compiled extension with no inline type information, so pyright reports `"etree" is unknown import symbol` in `server/Utils/svg_manipulator.py`. `types-lxml` provides the stubs. The project rule is never to silence findings with `# type: ignore` or config ignores.
- **Official page:** https://pypi.org/project/types-lxml/
- **Compatibility (from the project page):** supports lxml 6.0 and 5.4, and is validated against pyright ≥ 1.1.406. This project uses lxml 6.1 and pyright 1.1.414.

## Install

```powershell
# from backend/
uv add --dev types-lxml
```

## Notes

- This package is for type checking only. It has no runtime effect, so it doesn't belong in the production `requirements.txt`.
- Use the default `types-lxml` build. `types-lxml-multi-subclass` is only needed when you create several lxml element subclasses, which this project doesn't do.
