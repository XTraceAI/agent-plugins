MAX_ATTEMPTS = 3

def attempts_for(kind):
    return MAX_ATTEMPTS if kind == "transient" else 1
