// Pure split-tree engine for the canvas window. No DOM, no globals beyond
// the single window.SplitTree export at the end — keeps it node-extractable.
function stEmpty() { return null; }
function stInsertFirst(tree, key) {
  if (tree) throw new Error("insertFirst on non-empty tree");
  return { leaf: key };
}
window.SplitTree = { empty: stEmpty, insertFirst: stInsertFirst };
