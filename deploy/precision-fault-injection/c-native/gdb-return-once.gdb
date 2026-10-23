set pagination off
set confirm off
break *$agenticsre_target
commands
  silent
  printf "injection_event=gdb_return_override symbol=stable_operation return=-88\n"
  return (int)-88
  disable 1
  detach
  quit
end
continue
