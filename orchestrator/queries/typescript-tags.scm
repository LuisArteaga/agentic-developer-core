; Adapted from Aider's typescript-tags.scm (Apache License 2.0).
; Modified: reference captures removed (definition-only for outline extraction).
; Shared by both .ts (typescript grammar) and .tsx (tsx grammar) — declaration
; node types are identical across both grammars.
; Original source: https://github.com/Aider-AI/aider/blob/main/aider/queries/tree-sitter-language-pack/typescript-tags.scm

(function_declaration name: (identifier) @name.definition.function) @definition.function
(method_definition name: (property_identifier) @name.definition.method) @definition.method
(class_declaration name: (type_identifier) @name.definition.class) @definition.class
(interface_declaration name: (type_identifier) @name.definition.interface) @definition.interface
(type_alias_declaration name: (type_identifier) @name.definition.type) @definition.type
(enum_declaration name: (identifier) @name.definition.enum) @definition.enum
