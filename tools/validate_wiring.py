import ast
import os
import sys

# Constants
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEXUS_ARK_PATH = os.path.join(PROJECT_ROOT, "nexus_ark.py")
UI_HANDLERS_PATH = os.path.join(PROJECT_ROOT, "ui_handlers.py")

def parse_nexus_ark():
    """
    Parses nexus_ark.py to find event handler hookups.
    Returns a dictionary: { 'handler_name': expected_output_count }
    """
    with open(NEXUS_ARK_PATH, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())

    wiring_map = {}
    variable_map = {} # Stores lengths of known list variables

    class NexusVisitor(ast.NodeVisitor):
        def visit_Assign(self, node):
            # Resolve assignments like "my_list = [a, b] + [c]"
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                var_name = node.targets[0].id
                length = self._resolve_length(node.value)
                if length is not None:
                    variable_map[var_name] = length
            self.generic_visit(node)

        def _resolve_length(self, node):
            if isinstance(node, ast.List):
                return len(node.elts)
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                left_len = self._resolve_length(node.left)
                right_len = self._resolve_length(node.right)
                if left_len is not None and right_len is not None:
                    return left_len + right_len
            elif isinstance(node, ast.Name):
                return variable_map.get(node.id)
            return None

        def visit_Call(self, node):
            # Check for .click, .change, .submit, .then calls
            if isinstance(node.func, ast.Attribute) and node.func.attr in ['click', 'change', 'submit', 'then', 'select', 'edit', 'load']:
                fn_name = None
                outputs_count = 0

                # Check keywords for 'fn' and 'outputs'
                for keyword in node.keywords:
                    if keyword.arg == 'fn':
                        # Case: fn=ui_handlers.some_function
                        if isinstance(keyword.value, ast.Attribute) and isinstance(keyword.value.value, ast.Name):
                            if keyword.value.value.id == 'ui_handlers':
                                fn_name = keyword.value.attr
                        # Case: fn=lambda ...: ui_handlers.some_function(...)
                        elif isinstance(keyword.value, ast.Lambda):
                            # Try to find the call inside the lambda
                            if isinstance(keyword.value.body, ast.Call):
                                func_node = keyword.value.body.func
                                if isinstance(func_node, ast.Attribute) and isinstance(func_node.value, ast.Name):
                                    if func_node.value.id == 'ui_handlers':
                                        fn_name = func_node.attr

                    elif keyword.arg == 'outputs':
                        # Case: outputs=[component1, component2] (List)
                        length = self._resolve_length(keyword.value)
                        if length is not None:
                            outputs_count = length
                        # Case: outputs=component (Single item fallback, often a Name or Attribute)
                        elif isinstance(keyword.value, (ast.Name, ast.Attribute)):
                             if isinstance(keyword.value, ast.Name) and keyword.value.id in variable_map:
                                 outputs_count = variable_map[keyword.value.id]
                             else:
                                 outputs_count = 1 # Assume single component if not a known list
                        else:
                            outputs_count = -1 

                if fn_name and outputs_count > 0:
                    wiring_map[fn_name] = outputs_count
            
            self.generic_visit(node)

    NexusVisitor().visit(tree)
    return wiring_map

def parse_ui_handlers():
    """
    Parses ui_handlers.py to find function definitions and their return counts.
    Returns a dictionary: { 'handler_name': { 'returns': [count1, count2...], 'expected_count': val } }
    """
    with open(UI_HANDLERS_PATH, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())

    handlers_info = {}

    class HandlerVisitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            returns = []
            expected_count = None
            
            # Check arguments for 'expected_count' default value
            for arg in node.args.args:
                if arg.arg == 'expected_count':
                    defaults_offset = len(node.args.args) - len(node.args.defaults)
                    idx = node.args.args.index(arg)
                    if idx >= defaults_offset:
                        default_val = node.args.defaults[idx - defaults_offset]
                        if isinstance(default_val, ast.Constant):
                            expected_count = default_val.value

            # Check return/yield statements
            has_yield = False
            for child in ast.walk(node):
                if isinstance(child, ast.Yield) or isinstance(child, ast.YieldFrom):
                    has_yield = True
                    break

            for child in ast.walk(node):
                if isinstance(child, ast.Return):
                    if child.value is None:
                        # If it's a generator, an empty return just stops iteration, it's not a value yielding 0 items.
                        if has_yield:
                            continue
                        returns.append(0)
                        continue
                    
                    # Case: return _ensure_output_count(..., N)
                    if isinstance(child.value, ast.Call) and isinstance(child.value.func, ast.Name) and child.value.func.id == '_ensure_output_count':
                        if len(child.value.args) >= 2:
                            second_arg = child.value.args[1]
                            if isinstance(second_arg, ast.Name):
                                if second_arg.id == 'expected_count' and expected_count is not None:
                                     returns.append(expected_count)
                                else:
                                     returns.append("DYNAMIC_VAR") 
                            elif isinstance(second_arg, ast.Constant):
                                returns.append(second_arg.value)
                            else:
                                returns.append("DYNAMIC")
                        continue

                    # Case: return (a, b, c) or return [a, b, c]
                    if isinstance(child.value, (ast.Tuple, ast.List)):
                        returns.append(len(child.value.elts))
                    elif isinstance(child.value, ast.Call):
                         returns.append("DYNAMIC_CALL")
                    else:
                        returns.append("UNKNOWN")
                
                elif isinstance(child, ast.Yield):
                     if isinstance(child.value, (ast.Tuple, ast.List)):
                        returns.append(len(child.value.elts))
                     else:
                         # yield single_item -> 1 item
                        returns.append(1)
            
            # If function is a generator but has no yields found by walk (e.g. only YieldFrom), we might need to be careful
            # But here we just filter the empty returns.

            handlers_info[node.name] = {
                'returns': returns,
                'expected_count': expected_count
            }

    HandlerVisitor().visit(tree)
    return handlers_info

def main():
    print("--- Nexus Ark Wiring Validator ---")
    nexus_wiring = parse_nexus_ark()
    handlers_info = parse_ui_handlers()
    
    errors = []
    warnings = []

    for handler_name, required_count in nexus_wiring.items():
        if required_count == -1:
            warnings.append(f"[SKIP] {handler_name}: Used with a variable list in outputs, cannot verify statically.")
            continue
            
        if handler_name not in handlers_info:
            warnings.append(f"[WARN] {handler_name}: Defined in UI but not found in ui_handlers.py (or imported under different name).")
            continue
        
        info = handlers_info[handler_name]
        
        # Check explicit expected_count defined in function signature
        if info['expected_count'] is not None:
            if info['expected_count'] != required_count:
                errors.append(f"[FAIL] {handler_name}: Signature default expected_count={info['expected_count']} but UI requires {required_count}.")
        
        # Check return statements
        for ret_val in info['returns']:
            if ret_val == "DYNAMIC_VAR":
                 # Usually safe if it matches the argument name logic, assuming the argument was populated correctly
                 pass
            elif isinstance(ret_val, int):
                if ret_val != required_count:
                    errors.append(f"[FAIL] {handler_name}: Returns {ret_val} items, but UI defined {required_count} outputs.")
            elif ret_val == "UNKNOWN":
                warnings.append(f"[INFO] {handler_name}: Returns a non-literal value, cannot verify count statically.")

    print(f"\nChecked {len(nexus_wiring)} connections.")
    
    if warnings:
        print("\n--- Warnings ---")
        for w in warnings: print(w)
        
    if errors:
        print("\n--- ERRORS FOUND ---")
        for e in errors: print(e)
        sys.exit(1)
    else:
        print("\n✅ No critical wiring errors found!")

if __name__ == "__main__":
    main()
