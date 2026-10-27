//! Run a command in the "ask outside" jail of a folder (a check by hand).
fn main() {
    let a: Vec<String> = std::env::args().skip(1).collect();
    let root = std::path::PathBuf::from(&a[0]);
    let argv = chuck_sandbox::Jail::new(&root).keep_background().guarded()
        .argv(&chuck_sandbox::bwrap().expect("bwrap"), &["bash".into(), "-c".into(), a[1].clone()]);
    let st = std::process::Command::new(&argv[0]).args(&argv[1..]).current_dir(&root).status().unwrap();
    std::process::exit(st.code().unwrap_or(1));
}
