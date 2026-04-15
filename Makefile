.PHONY: sast

sast:
	pysemgrep --config p/security-audit .
