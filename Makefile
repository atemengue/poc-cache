.PHONY: up down wipe status check smoke full test rust-test build
up:
	./poc up --replicas 2
build:
	./poc build
down:
	./poc down
wipe:
	./poc down --volumes
status:
	./poc status
check:
	./poc check
smoke:
	./poc campaign --profile smoke --rate 100 --duration 15
full:
	./poc campaign --profile full --rate 150 --duration 25
test:
	./poc test
rust-test:
	./poc test-rust
