// Pure split-tree engine for the canvas window. No DOM, no globals beyond
// the single window.SplitTree export at the end — keeps it node-extractable.
function stEmpty() { return null; }
function stInsertFirst(tree, key) {
  if (tree) throw new Error("insertFirst on non-empty tree");
  return { leaf: key };
}
function stSplitLeaf(tree, targetKey, newKey, dir) {
  var orient = (dir === "left" || dir === "right") ? "row" : "col";
  var newLeaf = { leaf: newKey };
  var replace = function(node) {
    if (node.leaf !== undefined) {
      if (node.leaf !== targetKey) return node;
      var children = (dir === "left" || dir === "top")
        ? [newLeaf, { leaf: targetKey }]
        : [{ leaf: targetKey }, newLeaf];
      return { split: orient, ratios: [0.5, 0.5], children: children };
    }
    return { split: node.split, ratios: node.ratios.slice(),
             children: node.children.map(replace) };
  };
  return replace(tree);
}
window.SplitTree = { empty: stEmpty, insertFirst: stInsertFirst, splitLeaf: stSplitLeaf };
