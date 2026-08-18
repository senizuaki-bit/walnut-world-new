extends PanelContainer

@onready var code_editor: CodeEdit = %CodeEditor

func _ready() -> void:
	code_editor.syntax_highlighter = CodeHighlighter.new()
	var highlighter := code_editor.syntax_highlighter as CodeHighlighter
	highlighter.add_keyword_color("void", Color("b56cff"))
	highlighter.add_keyword_color("for", Color("b56cff"))
	highlighter.add_keyword_color("while", Color("b56cff"))
	highlighter.add_keyword_color("if", Color("b56cff"))
	highlighter.add_member_keyword_color("move", Color("319795"))
	highlighter.add_member_keyword_color("water", Color("319795"))
