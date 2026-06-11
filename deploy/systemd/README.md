# Always-on deployment (systemd user units)

One-time setup, per node (allows user services to run without a login session):

    loginctl enable-linger casm        # corr1 AND corr2

Install + start (corr1):

    mkdir -p ~/.config/systemd/user
    cp ~/software/dev/casm_t2/deploy/systemd/t2d.service ~/.config/systemd/user/
    cp ~/software/dev/casm_t3/deploy/systemd/{t3-dump-plotter-corr1,t3-collect,t3-janitor}.service ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now t2d t3-dump-plotter-corr1 t3-collect t3-janitor

corr2:

    mkdir -p ~/.config/systemd/user
    cp ~/software/dev/casm_t3/deploy/systemd/t3-dump-plotter-corr2.service ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now t3-dump-plotter-corr2

Stop the tmux/nohup equivalents first (t2watch, t2d, t3plot, t3collect tmux
sessions on corr1; setsid t3-dump-plotter on corr2). Status/logs:

    systemctl --user status t2d
    journalctl --user -u t2d -f      # plus the append: log files as before
