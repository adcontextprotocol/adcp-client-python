"""The class names ``datamodel-code-generator`` assigns, asked rather than restated.

The generator names a schema ROOT and a named NESTED object by two DIFFERENT
rules, and this repo restated both rather than calling either. Three
restatements existed:

* ``post_generate_fixes._schema_title_to_class_name`` — a faithful copy of the
  NESTED rule, used for the root as well. Wrong on every acronym for 94 of the
  946 roots where the generated module declares a class to compare against
  (salesagent-5pe83);
* ``task_message_lattice.mangle_schema_title`` — a ``str.capitalize`` copy of the
  ROOT rule. Wrong for 5 of the same 946: ``A2UI Component`` lower-cases a
  digit-split token to ``A2ui`` where the generator gives ``A2Ui``, and a title
  that is ALREADY a Python identifier is flattened rather than kept
  (salesagent-8qela.20);
* one more that is not a restatement of a generator fact and stays put:
  ``union_branch_names.pascal_case``, which names a union BRANCH — a name this
  repo's own emitter chooses, not one the generator assigns.

Both rules now live here, each a call into the generator that owns the answer,
so there is one representation per fact and a generator that changes its mind
changes these with it. ``datamodel-code-generator`` is pinned exactly (0.64.0
in both the dev extra and the dev group), so a rule change arrives with a
deliberate bump, and ``tests/test_generated_hierarchy.py``'s
``test_the_root_class_name_derived_from_a_title_is_the_name_the_module_declares``
grades the derivation against the generated tree on every run.

This module has no repo-local imports on purpose: ``post_generate_fixes``
imports ``task_message_lattice``, so a shared helper in either of them would be
a cycle.
"""

from __future__ import annotations

from datamodel_code_generator.parser.base import title_to_class_name
from datamodel_code_generator.reference import ModelResolver


def generated_nested_class_name(title: object) -> str:
    """The class name the generator gives a named NESTED schema object.

    ``ModelResolver.default_class_name_generator`` resolves an upper-camel name
    and PRESERVES an acronym: ``"VAST Asset"`` is ``VASTAsset``. The restatement
    this replaced — ``"".join(word[:1].upper() + word[1:] for word in words)`` —
    was a faithful copy of this rule, and was correct wherever it was used for a
    nested object. Its defect was being used for the root as well.
    """
    return ModelResolver().default_class_name_generator(str(title))


def generated_root_class_name(title: object) -> str:
    """The class name the generator gives a schema ROOT.

    A root goes through one step a nested object does not:
    ``JsonSchemaParser._resolve_root_model_name`` title-cases a title that is not
    already a Python identifier, so ``"Get AdCP Capabilities Response"`` becomes
    ``GetAdcpCapabilitiesResponse`` and not ``GetAdCPCapabilitiesResponse``. A
    title that IS already an identifier is kept verbatim —
    ``GetCreativeFeaturesSuccess`` is one, and title-casing it unconditionally
    yields ``Getcreativefeaturessuccess``.

    Both steps are the generator's own, called in the generator's order.
    """
    name = str(title)
    if not ModelResolver.validate_name(name):
        name = title_to_class_name(name)
    return generated_nested_class_name(name)
