//! Quitting ends every command any agent started. (Its own test binary:
//! kill_all ends every command in the process, so it can't share one with
//! tests that run commands in parallel.)

use chuck_tools::{bash, kill_all, running, RunOpts};
use std::time::{Duration, Instant};

#[test]
fn kill_all_ends_every_running_command_and_its_children() {
    let d = std::env::temp_dir().join(format!("chuck-killall-{}", std::process::id()));
    std::fs::create_dir_all(&d).unwrap();
    let t0 = Instant::now();
    let mut hs = Vec::new();
    for i in 0..3 {
        let d = d.clone();
        // a child process inside the command, too: the whole group goes
        hs.push(std::thread::spawn(move || bash(&format!("(sleep 30; touch child{i}) & sleep 30; touch parent{i}"),
                                                RunOpts::new(&d))));
    }
    while running() < 3 && t0.elapsed() < Duration::from_secs(5) {
        std::thread::sleep(Duration::from_millis(20));
    }
    assert_eq!(running(), 3);
    kill_all();
    for h in hs {
        assert_ne!(h.join().unwrap().exit, 0);
    }
    assert!(t0.elapsed() < Duration::from_secs(5), "{:?}", t0.elapsed());
    assert_eq!(running(), 0);
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(std::fs::read_dir(&d).unwrap().count(), 0, "nothing ran on after the kill");
    let _ = std::fs::remove_dir_all(&d);
}
