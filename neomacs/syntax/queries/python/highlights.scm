; Highlighting for Python, written for Neomacs.
; Capture names are the editor's fixed vocabulary; each is also a face name.
; A later pattern wins over an earlier one for the same text.

(comment) @comment

(string) @string

(integer) @number
(float) @number

[(none) (true) (false) (ellipsis)] @constant
((identifier) @constant
 (#match? @constant "^(self|cls|NotImplemented|Ellipsis)$"))

[
  "and" "as" "assert" "async" "await" "break" "class" "continue" "def" "del"
  "elif" "else" "except" "finally" "for" "from" "global" "if" "import" "in"
  "is" "lambda" "nonlocal" "not" "or" "pass" "raise" "return" "try" "while"
  "with" "yield" "match" "case"
] @keyword

(function_definition name: (identifier) @function)
(class_definition name: (identifier) @type)
(decorator) @decorator
