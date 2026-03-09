import sys
import os
sys.path.append(os.getcwd())
import restore_graph
import restore_graph_part2

full_content = restore_graph.content_part1 + restore_graph_part2.content_part2
with open('agent/graph.py', 'w', encoding='utf-8') as f:
    f.write(full_content)
print('File restored successfully.')
