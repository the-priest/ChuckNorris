//! Definitions, references and rename through a language server: a fake one
//! that checks the protocol (tests/fake_lsp.py), and pyright when it's
//! installed — plus the fallbacks when there's none.

use chuck_code::lsp::{self, Manager};
use chuck_code::syntax;
use std::path::{Path, PathBuf};
use std::time::Duration;

const T: Duration = Duration::from_secs(60);

fn project(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("chuck-lsp-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(d.join("pkg")).unwrap();
    std::fs::write(d.join("pkg/__init__.py"), "").unwrap();
    std::fs::write(d.join("pkg/store.py"), "class Store:\n    def save(self, x):\n        return x\n\n\
        def make_store():\n    return Store()\n").unwrap();
    // an emoji before the name: columns must count UTF-16 units
    std::fs::write(d.join("pkg/app.py"), "from pkg.store import make_store, Store\n\nlabel = \"😀\"; s = make_store()\n\
        s.save(1)\nsaver = Store()\n").unwrap();
    d.canonicalize().unwrap()
}

fn fake(m: &Manager) -> bool {
    if chuck_mcp_like_which("python3").is_none() {
        return false;
    }
    m.set_server("python", vec!["python3".into(), concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fake_lsp.py").into()]);
    true
}

fn chuck_mcp_like_which(cmd: &str) -> Option<PathBuf> {
    std::env::split_paths(&std::env::var_os("PATH")?).map(|d| d.join(cmd)).find(|p| p.is_file())
}

fn apply(root: &Path, plan: &[(String, String, String)]) {
    for (rel, _, after) in plan {
        std::fs::write(root.join(rel), after).unwrap();
    }
}

#[test]
fn by_meaning_through_a_server() {
    let m = Manager::new();
    if !fake(&m) {
        return;
    }
    let root = project("fake");
    let (defs, how) = lsp::definitions(Some(&m), &root, "make_store", T).unwrap();
    assert_eq!(how, "python3");
    assert_eq!(defs.iter().map(|d| (d.path.as_str(), d.line, d.kind.as_str())).collect::<Vec<_>>(),
               [("pkg/store.py", 5, "function")], "the library's symbol is dropped");
    let (defs, _) = lsp::definitions(Some(&m), &root, "Store.save", T).unwrap();
    assert_eq!((defs[0].path.as_str(), defs[0].line, defs[0].container.as_str()), ("pkg/store.py", 2, "Store"));

    let (refs, how, _) = lsp::references(Some(&m), &root, "make_store", T, 50).unwrap();
    assert_eq!(how, "python3");
    assert_eq!(refs.iter().map(|r| (r.path.as_str(), r.line)).collect::<Vec<_>>(), [("pkg/app.py", 1), ("pkg/app.py", 3)]);

    let (plan, _) = lsp::rename(Some(&m), &root, "make_store", "build_store", T).unwrap();
    assert_eq!(plan.iter().map(|p| p.0.as_str()).collect::<Vec<_>>(), ["pkg/app.py", "pkg/store.py"]);
    apply(&root, &plan);
    let app = std::fs::read_to_string(root.join("pkg/app.py")).unwrap();
    assert!(app.contains("label = \"😀\"; s = build_store()") && app.contains("import build_store, Store"), "{app}");
    assert!(std::fs::read_to_string(root.join("pkg/store.py")).unwrap().contains("def build_store():"));

    // the server sees the change on its next question (didChange, not a stale copy)
    let (defs, _) = lsp::definitions(Some(&m), &root, "build_store", T).unwrap();
    assert_eq!(defs.len(), 1);
    assert!(lsp::definitions(Some(&m), &root, "make_store", T).unwrap().0.is_empty());

    // refusals: an ambiguous name, a bad new name
    std::fs::write(root.join("pkg/other.py"), "def build_store():\n    pass\n").unwrap();
    let e = lsp::rename(Some(&m), &root, "build_store", "x", T).unwrap_err();
    assert!(e.0.contains("defined 2 times"), "{e}");
    assert!(lsp::rename(Some(&m), &root, "Store", "1bad", T).unwrap_err().0.contains("isn't a valid name"));
    m.stop_all();
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn a_server_that_disagrees_with_the_file_changes_nothing() {
    let m = Manager::new();
    if !fake(&m) {
        return;
    }
    let root = project("stale");
    m.set_server("python", vec!["env".into(), "FAKE_LSP_STALE=1".into(), "python3".into(),
                                concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fake_lsp.py").into()]);
    let e = lsp::rename(Some(&m), &root, "make_store", "build_store", T).unwrap_err();
    assert!(e.0.contains("nothing was changed"), "{e}");
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn without_a_server_syntax_answers_and_says_so() {
    let root = project("none");
    let (defs, how) = lsp::definitions(None, &root, "save", T).unwrap();
    assert_eq!(how, "syntax (no language server)");
    assert_eq!((defs[0].path.as_str(), defs[0].line, defs[0].kind.as_str()), ("pkg/store.py", 2, "method"));
    let (refs, how, _) = lsp::references(None, &root, "save", T, 50).unwrap();
    assert!(how.starts_with("text search"));
    assert_eq!(refs.iter().map(|r| (r.path.as_str(), r.line)).collect::<Vec<_>>(), [("pkg/app.py", 4)], "not `saver`");
    let e = lsp::rename(None, &root, "make_store", "build_store", T).unwrap_err();
    assert!(e.0.contains("needs a language server") && e.0.contains("pyright"), "{e}");
    assert!(syntax::repo_map(&root, 5000).contains("pkg/store.py (6 lines): Store[save], make_store()"));
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn pyright_for_real_when_installed() {
    let m = Manager::new();
    if lsp::server_for("python", Path::new("/")).is_none() {
        return;
    }
    let root = project("pyright");
    let (defs, how) = lsp::definitions(Some(&m), &root, "make_store", T).unwrap();
    if how.starts_with("syntax") {
        eprintln!("pyright is installed but didn't answer here: {:?}", m.failed.lock().unwrap());
        return; // (no node, say): the fallback is tested above
    }
    eprintln!("answered by {how}");
    assert_eq!((defs[0].path.as_str(), defs[0].line), ("pkg/store.py", 5), "{how}");
    let (refs, _, _) = lsp::references(Some(&m), &root, "make_store", T, 50).unwrap();
    assert!(refs.iter().any(|r| r.path == "pkg/app.py" && r.line == 3), "{refs:?}");
    let (plan, _) = lsp::rename(Some(&m), &root, "make_store", "build_store", T).unwrap();
    apply(&root, &plan);
    let app = std::fs::read_to_string(root.join("pkg/app.py")).unwrap();
    assert!(app.contains("s = build_store()") && !app.contains("make_store"), "{app}");
    m.stop_all();
    let _ = std::fs::remove_dir_all(&root);
}
