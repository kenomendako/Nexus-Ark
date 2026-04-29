import ast
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEXUS_ARK_PATH = os.path.join(PROJECT_ROOT, "nexus_ark.py")

def resolve_items(node, variable_map):
    if isinstance(node, ast.List):
        items = []
        for elt in node.elts:
            if isinstance(elt, ast.Name):
                items.append(elt.id)
            elif isinstance(elt, ast.Attribute):
                items.append(elt.attr)
            elif isinstance(elt, ast.Constant):
                items.append(str(elt.value))
            else:
                items.append(f"UNKNOWN({type(elt).__name__})")
        return items
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left_items = resolve_items(node.left, variable_map)
        right_items = resolve_items(node.right, variable_map)
        return left_items + right_items
    elif isinstance(node, ast.Name):
        return variable_map.get(node.id, [f"REF:{node.id}"])
    return []

def main():
    with open(NEXUS_ARK_PATH, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())

    variable_map = {} # name -> list of items

    class ItemVisitor(ast.NodeVisitor):
        def visit_Assign(self, node):
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                var_name = node.targets[0].id
                items = resolve_items(node.value, variable_map)
                variable_map[var_name] = items
            self.generic_visit(node)

    ItemVisitor().visit(tree)

    if 'initial_load_outputs' in variable_map:
        print(f"initial_load_outputs Count: {len(variable_map['initial_load_outputs'])}")
        for i, item in enumerate(variable_map['initial_load_outputs']):
            print(f"{i}: {item}")
    else:
        print("initial_load_outputs not found!")

if __name__ == "__main__":
    main()
