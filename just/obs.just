# just/obs.just — read the trace. Every recipe is a query against one sqlite file.
#
# You watch from outside; nothing here reaches into a run. The tracer writes as
# work happens (WAL, so reads never block the writer), which is why `tail` on a
# live run is safe and why a killed run still shows a closed session.
#
# The db lives in the TARGET repo, not here: one factory, many repos, one trace
# each. Pass REPO when you are not standing in it.
# A module's recipes also run from the ROOT justfile's directory, so a repo
# argument defaults to the invocation directory rather than to ".".
set working-directory := '.'
set positional-arguments
set dotenv-load

# list the trace commands
default:
    @just --list obs

# where a repo's trace db is
db REPO=invocation_directory():
    @echo "{{REPO}}/.sssf/data/sssf.db"

# recent runs: workflow, status, phases, spend
sessions REPO=invocation_directory() LIMIT="20":
    #!/usr/bin/env bash
    set -euo pipefail
    db="{{REPO}}/.sssf/data/sssf.db"
    [ -f "$db" ] && sqlite3 -header -column "$db" "
        select substr(adw_id,1,8) as adw_id, adw_name as workflow, status,
               (select count(*) from phases p where p.adw_id = s.adw_id) as phases,
               printf('%.4f', total_cost) as cost, total_tokens as tokens,
               substr(request,1,44) as request
        from sessions s where archived = 0
        order by started_at desc limit {{LIMIT}};" \
      || echo "no trace db yet at $db — run something first"

# every phase of one run, in order, with timings
phases ADW_ID REPO=invocation_directory():
    #!/usr/bin/env bash
    set -euo pipefail
    sqlite3 -header -column "{{REPO}}/.sssf/data/sssf.db" "
        select seq, name, kind, owner, status, attempt,
               round((julianday(ended_at) - julianday(started_at)) * 86400, 1) as secs,
               substr(description,1,52) as description
        from phases where adw_id like '{{ADW_ID}}%' order by seq;"

# the event stream for one run: agent starts, tool calls, gates, handoffs
events ADW_ID REPO=invocation_directory() LIMIT="80":
    #!/usr/bin/env bash
    set -euo pipefail
    sqlite3 -header -column "{{REPO}}/.sssf/data/sssf.db" "
        select substr(started_at,12,12) as at, type, name,
               substr(payload_json,1,68) as payload
        from events where adw_id like '{{ADW_ID}}%'
        order by rowid desc limit {{LIMIT}};" | tail -r 2>/dev/null \
      || sqlite3 -header -column "{{REPO}}/.sssf/data/sssf.db" "
        select substr(started_at,12,12) as at, type, name,
               substr(payload_json,1,68) as payload
        from events where adw_id like '{{ADW_ID}}%' order by rowid limit {{LIMIT}};"

# follow a live run — polls, because the writer is another process
tail ADW_ID REPO=invocation_directory() EVERY="2":
    #!/usr/bin/env bash
    set -uo pipefail
    db="{{REPO}}/.sssf/data/sssf.db"
    seen=0
    while true; do
        rows=$(sqlite3 "$db" "select rowid || '  ' || type || '  ' || name ||
                              '  ' || substr(coalesce(payload_json,''),1,90)
                              from events where adw_id like '{{ADW_ID}}%'
                              and rowid > $seen order by rowid;")
        if [ -n "$rows" ]; then
            echo "$rows"
            seen=$(sqlite3 "$db" "select max(rowid) from events
                                  where adw_id like '{{ADW_ID}}%';")
        fi
        status=$(sqlite3 "$db" "select status from sessions
                                where adw_id like '{{ADW_ID}}%';")
        [ "$status" != "running" ] && { echo "── session $status"; break; }
        sleep {{EVERY}}
    done

# what every gate checked, and what it found — the claim-verification record
gates ADW_ID REPO=invocation_directory():
    #!/usr/bin/env bash
    set -euo pipefail
    sqlite3 -header -column "{{REPO}}/.sssf/data/sssf.db" "
        select p.seq, p.name as phase, g.gate, g.attempt,
               case g.passed when 1 then 'pass' else 'FAIL' end as result,
               substr(g.checks_json,1,60) as checks
        from gate_results g join phases p on p.phase_id = g.phase_id
        where g.adw_id like '{{ADW_ID}}%' order by p.seq, g.id;"

# the typed envelope each agent produced
envelopes ADW_ID REPO=invocation_directory():
    #!/usr/bin/env bash
    set -euo pipefail
    sqlite3 -header -column "{{REPO}}/.sssf/data/sssf.db" "
        select agent, output_type, attempt,
               case valid when 1 then 'valid' else 'INVALID' end as parse,
               substr(payload_json,1,60) as payload
        from envelopes where adw_id like '{{ADW_ID}}%' order by created_at;"

# spend, per agent and per model, across every run in this repo
costs REPO=invocation_directory():
    #!/usr/bin/env bash
    set -euo pipefail
    sqlite3 -header -column "{{REPO}}/.sssf/data/sssf.db" "
        select a.agent, a.model, count(*) as runs,
               printf('%.4f', sum(coalesce(json_extract(e.payload_json,'\$.cost'), 0)))
                   as cost
        from agent_sessions a
        left join events e on e.adw_id = a.adw_id
             and e.type = 'agent_end' and e.name = a.agent
        group by a.agent, a.model order by 4 desc;"

# processes a run started, and whether they are still alive
procs REPO=invocation_directory():
    #!/usr/bin/env bash
    set -euo pipefail
    sqlite3 "{{REPO}}/.sssf/data/sssf.db" "
        select substr(adw_id,1,8), kind, coalesce(name,''), pid, command
        from processes where ended_at is null order by started_at;" \
    | while IFS='|' read -r adw kind name pid cmd; do
        # A pid outliving its row gets reused, so the command is checked before
        # anything is reported as live — a stale row must never look running.
        live=$(ps -p "$pid" -o command= 2>/dev/null || true)
        case "$live" in
            *pi*|*sf.py*|*python*) echo "  live  $adw $kind $pid  $cmd" ;;
            "")                    echo "  gone  $adw $kind $pid  (row still open)" ;;
            *)                     echo "  reused $adw $kind $pid is now '$live' — NOT ours" ;;
        esac
    done

# the artifacts one run left on disk: prompts, envelopes, diffs, check logs
artifacts ADW_ID REPO=invocation_directory():
    @find {{REPO}}/.sssf/data/sessions/{{ADW_ID}}* -type f 2>/dev/null | sort \
      || echo "no session dir for {{ADW_ID}}"
