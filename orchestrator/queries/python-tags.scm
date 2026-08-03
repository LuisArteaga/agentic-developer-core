; Adapted from Aider's python-tags.scm (Apache License 2.0).
; Modified: reference captures removed (definition-only for outline extraction).
; Original source: https://github.com/Aider-AI/aider/blob/main/aider/queries/tree-sitter-language-pack/python-tags.scm

(class_definition
  name: (identifier) @name.definition.class) @definition.class

(function_definition
  name: (identifier) @name.definition.function) @definition.function
