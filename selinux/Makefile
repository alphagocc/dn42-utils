# 编译 dn42ctl 的 SELinux 策略模块。
#
# 依赖 selinux-policy-devel,它提供 /usr/share/selinux/devel/Makefile
# 与 refpolicy 的接口头文件：
#
#     sudo dnf install selinux-policy-devel
#
# 安装与首次上线的完整步骤见 docs/architecture/selinux.md。

MODULE := dn42ctl
DEVEL  := /usr/share/selinux/devel/Makefile

RELABEL_PATHS := /usr/local/bin/dn42ctl /opt/dn42ctl /etc/dn42ctl \
                 /var/lib/dn42ctl /etc/bird /etc/systemd/network

.PHONY: all install permissive enforcing uninstall clean

all: $(MODULE).pp

$(MODULE).pp: $(MODULE).te $(MODULE).fc $(MODULE).if
	$(MAKE) -f $(DEVEL) $(MODULE).pp

install: $(MODULE).pp
	semodule -i $(MODULE).pp
	restorecon -RvF $(RELABEL_PATHS)

# 首次上线先跑宽容模式,收集完 AVC 再切回强制。
permissive:
	semanage permissive -a dn42ctl_server_t
	semanage permissive -a dn42ctl_agent_t

enforcing:
	semanage permissive -d dn42ctl_server_t
	semanage permissive -d dn42ctl_agent_t

uninstall:
	semodule -r $(MODULE)
	restorecon -RvF $(RELABEL_PATHS)

clean:
	rm -rf tmp $(MODULE).pp $(MODULE).mod $(MODULE).mod.fc
