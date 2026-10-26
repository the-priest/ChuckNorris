//! chuck-ui — the GTK4 window: talk to the Lead, watch the fleet work, see
//! the Done gate fill in.

mod md;
mod style;
mod window;

pub use window::Options;

use adw::prelude::*;

/// Open the window; returns when it's closed.
pub fn run(opts: Options) -> i32 {
    let app = adw::Application::builder().application_id("org.thepriest.chuck.fleet")
        .flags(gtk::gio::ApplicationFlags::NON_UNIQUE).build();
    app.connect_activate(move |app| window::build(app, opts.clone()));
    app.run_with_args::<&str>(&[]).value()
}
