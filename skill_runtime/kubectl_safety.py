"""Parse read-only kubectl requests without accepting shell programs."""
import re
import shlex

READ_ONLY_VERBS = {"get", "describe", "logs", "top", "events", "cluster-info", "version", "explain", "api-resources", "api-versions"}


def readonly_kubectl_args(command: str, namespace: str = "") -> list[str]:
    if namespace and not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", namespace):
        raise ValueError("Invalid Kubernetes namespace")
    # No pipes, substitutions, redirects or multi-command expressions. JSONPath
    # quoting and label selectors remain supported by shlex, without a shell.
    if any(c in command for c in (";", "|", "&", "`", "$", "\n", "\r", "<", ">")):
        raise ValueError("Shell syntax is not allowed in read-only kubectl")
    args = shlex.split(command)
    if not args or args[0] not in READ_ONLY_VERBS:
        raise ValueError("Only read-only kubectl verbs are allowed")
    for arg in args:
        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 2:
            raise ValueError("Combined or compact short options are not allowed")
        if arg.startswith(("--kubeconfig", "--context", "--server", "--token", "--client-", "--certificate-", "--as", "-s")):
            raise ValueError("Cluster credentials and context are controlled by the host")
    for i, arg in enumerate(args):
        value = None
        if arg in {"-n", "--namespace"}:
            if i + 1 == len(args):
                raise ValueError("Missing namespace value")
            value = args[i + 1]
        elif arg.startswith("--namespace="):
            value = arg.split("=", 1)[1]
        elif arg.startswith("-n") and len(arg) > 2:
            value = arg[2:]
        if value is not None:
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value):
                raise ValueError("Invalid Kubernetes namespace")
            if namespace and value != namespace:
                raise ValueError("Command namespace differs from request scope")
    has_namespace = any(a in {"-n", "--namespace", "-A", "--all-namespaces"} or a.startswith(("--namespace=", "-n")) for a in args)
    if namespace and not has_namespace:
        args.extend(["-n", namespace])
    return args
