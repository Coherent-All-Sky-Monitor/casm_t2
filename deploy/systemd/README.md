# Always-on deployment (systemd user units)

One-time setup, per node (allows user services to run without a login session):

    loginctl enable-linger casm        # corr1 AND corr2

Install + start (corr1):

    mkdir -p ~/.config/systemd/user
    cp ~/software/dev/casm_t2/deploy/systemd/t2d.service ~/.config/systemd/user/
    cp ~/software/dev/casm_t3/deploy/systemd/{t3-dump-plotter-corr1,t3-collect,t3-janitor}.service ~/.config/systemd/user/
    cp ~/software/dev/casm_t3/deploy/systemd/t3-mask-janitor.{service,timer} ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now t2d t3-dump-plotter-corr1 t3-collect t3-janitor
    systemctl --user enable --now t3-mask-janitor.timer     # hourly sweep of hella's flag records

corr2:

    mkdir -p ~/.config/systemd/user
    cp ~/software/dev/casm_t3/deploy/systemd/t3-dump-plotter-corr2.service ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now t3-dump-plotter-corr2

`t3-mask-janitor` runs on corr1 only and sweeps the hella mask-record directory
on both nodes (it reaches corr2 over ssh, like `t3-janitor`), so corr2 needs
no unit for it. The record directory (`--mask-records` of the dump plotters,
`MASK_RECORD_DIR` of hella) must exist on both nodes before hella writes to it.

Stop the tmux/nohup equivalents first (t2watch, t2d, t3plot, t3collect tmux
sessions on corr1; setsid t3-dump-plotter on corr2). Status/logs:

    systemctl --user status t2d
    journalctl --user -u t2d -f      # the append-mode log files still exist
