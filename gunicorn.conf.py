workers = 1
threads = 4
timeout = 45
accesslog = None


def nworkers_changed(server, new_value, old_value):
    if server.cfg.preload_app or (old_value is None and new_value != 1):
        raise RuntimeError("O monitor exige um worker Gunicorn, com preload desativado.")
    if new_value > 1:
        server.num_workers = 1
        server.log.warning("O monitor mantém um único worker Gunicorn.")
