; Named units for Python, written for Neomacs.
; A decorated definition is one unit, decorators included.

(function_definition) @function
(decorated_definition definition: (function_definition)) @function
(class_definition) @class
(decorated_definition definition: (class_definition)) @class

(parameters (_) @parameter)
(lambda_parameters (_) @parameter)
(argument_list (_) @parameter)

[
  (expression_statement) (return_statement) (import_statement)
  (import_from_statement) (future_import_statement) (pass_statement)
  (break_statement) (continue_statement) (raise_statement) (assert_statement)
  (delete_statement) (global_statement) (nonlocal_statement) (type_alias_statement)
  (if_statement) (for_statement) (while_statement) (try_statement)
  (with_statement) (match_statement) (function_definition) (class_definition)
  (decorated_definition)
] @statement

(comment) @comment
(string) @string
