"""The one derivation for "which classes stand behind this name".

A schema root whose branches partition by requiredness is generated as a shared
base plus one class per branch, with the root's public name bound to a union over
the branches. Three places then need the same fact, and each got it wrong in its
own way before this existed:

* the ergonomic-coercion generator analyses a model's fields -- a union has
  none;
* the consolidated-export module rebuilds forward references -- a union has no
  ``model_rebuild``, and the generated comment beside those calls already said
  "only call model_rebuild() on actual classes, not Union type aliases" without
  any way to tell;
* a read-back clone copies the FIELDS of a root whose branches differ only in
  which of them are required, so it has to copy the base's -- which is why
  ``READBACK_CLONES`` in ``scripts/post_generate_fixes.py`` passes
  ``shared_base(CreativeManifest)`` and not the public name. (The third reader
  used to be ``_forward_compat``, which patched a base and then rebuilt every
  branch, because a subclass caches its parent's annotations when it is built.
  Nothing patches a field any more; the derivation outlived the reason for it.)

So the derivation lives here once. The base is read off an branch's MRO rather
than looked up by name or spelled out: the branches all name it, which is the
property the emitter's own tests grade, and reading it this way means this
module does not have to know how the base is spelled.
"""

from __future__ import annotations

from typing import Annotated, Any, get_args, get_origin

__all__ = ["branch_members", "classes_behind", "shared_base"]


def branch_members(declared: Any) -> tuple[Any, ...]:
    """The branch classes a split root's public name stands for, or ``()``.

    The alias is ``Annotated[A | B, WrapValidator(...)]``, because a refusal has
    to name the root's combinator. ``get_args`` on that returns the union and
    the metadata rather than the members, so every reader unwraps the annotation
    first. One owner for that, because three readers need it and each would get
    it subtly wrong: a reader that takes ``get_args(...)[0]`` and calls
    ``issubclass`` on it raises ``TypeError``, which is how this was found.
    """
    if get_origin(declared) is Annotated:
        declared = get_args(declared)[0]
    members = get_args(declared)
    if not members or not all(isinstance(member, type) for member in members):
        return ()
    return members


def classes_behind(declared: Any) -> tuple[Any, ...]:
    """Every class the name stands for, base first, or just the model itself.

    For a split root: the shared base followed by each branch, which is the
    order a patch-then-rebuild needs -- the base carries the change and every
    branch has to be rebuilt to see it.
    """
    members = branch_members(declared)
    if not members:
        return (declared,)
    return (members[0].__mro__[1], *members)


def shared_base(declared: Any) -> Any:
    """What carries the fields: the shared base for a split root, else the model.

    This is the target of anything that reads or patches a FIELD. The branches only
    change which fields are required, forbidden or pinned, so the field set is
    the base's and patching it once covers every branch.
    """
    return classes_behind(declared)[0]
