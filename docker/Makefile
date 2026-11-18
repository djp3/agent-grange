# Build-time git metadata baked into the image. Compose cannot run git, so it
# happens here. Use `make up` / `make build` instead of calling compose directly.
GIT_COMMIT := $(shell git rev-parse HEAD 2>/dev/null || echo unknown)
GIT_BRANCH := $(shell git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)
GIT_REPO   := $(shell git config --get remote.origin.url 2>/dev/null | sed -E 's|^(git@github\.com:\|https://github\.com/)||; s|\.git$$||')
GIT_DIRTY  := $(shell test -z "$$(git status --porcelain -- . 2>/dev/null)" && echo 0 || echo 1)
export GIT_COMMIT GIT_BRANCH GIT_REPO GIT_DIRTY

# Nearest pushed ancestor(s) of HEAD: the boundary between local-only commits
# and commits that exist on origin. GitHub has never seen an unpushed HEAD, so
# at launch entrypoint.py reasons about the tag relative to this instead.
# If nothing is unpushed there is no boundary, so fall back to HEAD itself.
GIT_PUSHED_BASE := $(shell git rev-list --boundary HEAD --not --remotes=origin 2>/dev/null | sed -n 's/^-//p' | tr '\n' ' ')
ifeq ($(strip $(GIT_PUSHED_BASE)),)
GIT_PUSHED_BASE := $(GIT_COMMIT)
endif
export GIT_PUSHED_BASE

# Which tag the container checks itself against at launch. Same default as
# docker-compose.yml; defined here too so the summary below can show it.
CHECK_TAG ?= agent-grange-latest
export CHECK_TAG

# Runtime: GitHub token so the container can query a private repo. If the
# variable is not already set, borrow the gh CLI's token.
GITHUB_TOKEN ?= $(shell gh auth token 2>/dev/null)
export GITHUB_TOKEN

# Display-only figures for the summary.
GIT_UNPUSHED    := $(shell git rev-list --count HEAD --not --remotes=origin 2>/dev/null || echo ?)
GIT_DIRTY_COUNT := $(shell git status --porcelain --untracked-files=all -- . 2>/dev/null | wc -l | tr -d ' ')
# No commas inside $(if ...): make would read them as argument separators.
DIRTY_TEXT      := $(if $(filter 1,$(GIT_DIRTY)),yes: $(GIT_DIRTY_COUNT) path(s) in this directory,no)

.PHONY: up build down logs ps info git-status update test test-sim test-host test-plain test-headscale

# Printed before every target.
git-status:
	@echo "---- git status: $(CURDIR) ----"
	@echo "  repo         $(GIT_REPO)"
	@echo "  branch       $(GIT_BRANCH)"
	@git --no-pager log -1 --format='  commit       %h  %s  (%cr)' 2>/dev/null || echo "  commit       unknown"
	@echo "  unpushed     $(GIT_UNPUSHED) commit(s) not on origin; pushed base: $$(echo $(GIT_PUSHED_BASE) | tr ' ' '\n' | cut -c1-12 | tr '\n' ' ')"
	@echo "  dirty        $(DIRTY_TEXT)"
	@git status --short --untracked-files=all -- . 2>/dev/null | head -10 | sed 's/^/               /'
	@test $(GIT_DIRTY_COUNT) -le 10 || echo "               ... and $$(( $(GIT_DIRTY_COUNT) - 10 )) more"
	@echo "  check tag    $(CHECK_TAG)"
	@echo "  token        $(if $(GITHUB_TOKEN),set,unset)"
	@echo "----------------------------------------"

up: git-status      ## build if needed, start, wait for healthy
	docker compose up -d --build --wait
build: git-status   ## build the image only
	docker compose build
down: git-status
	docker compose down
logs: git-status     ## follow the grange service
	@docker compose logs -f --no-log-prefix grange
ps: git-status
	@docker compose ps -a
info: git-status    ## just the summary
update: git-status  ## what to run when a container reports it is out of date
	git pull --ff-only
	$(MAKE) up

# ---------------------------------------------------------------- tests (R8.8)
# The unit tests run inside the built image, on the interpreter it ships, with
# tests/ bind-mounted read-only and the entrypoint (freshness check) bypassed.
IMAGE      := agent-grange:local
TEST_RUN    = docker run --rm --entrypoint python3 \
                -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/app \
                -v "$(CURDIR)/tests:/tests:ro" -w /tests $(IMAGE)

test: build          ## unit tests and the short simulation, inside the image
	$(TEST_RUN) -m unittest discover -s /tests -t /tests
test-host:           ## the same unit tests on the host interpreter; optional fast loop
	cd content && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s ../tests -t ../tests
test-sim:            ## hour-long simulations at design size (phase 3)
	@echo "test-sim: not built yet (plan phase 3)"; exit 1
test-plain:          ## five granges on a plain Docker network (phase 4)
	@echo "test-plain: not built yet (plan phase 4)"; exit 1
test-headscale:      ## local headscale, sidecars, tag discovery (phase 6)
	@echo "test-headscale: not built yet (plan phase 6)"; exit 1
