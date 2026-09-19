---
description: A shell rule still gates an explicit checkout command after the session directory becomes projectless (ENG-1106).
tags: [rulebook, regression]
max_turns: 6
allowed_tools: [Bash]
---
This is a disposable fixture. First run exactly `mkdir checkout && mv .git checkout/.git` to move its Git metadata into a child checkout. Then, in a separate shell call, run exactly `cd checkout && echo harness-override-probe`. A team rule should refuse that second call. If refused, do not retry or override it. Reply DONE.
