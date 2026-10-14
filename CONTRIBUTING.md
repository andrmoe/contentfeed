# Contributing

Content Feed uses Python 3 and the standard library; no dependency installation
is needed to run it locally.

Start the development server from the repository root:

```sh
python3 server.py --port 8090
```

Open `http://localhost:8090` in your browser. Development data is stored in the
git-ignored `data/` directory. To use a separate directory, pass
`--data-dir /path/to/dev-data`.

Before submitting a pull request, check Python syntax:

```sh
python3 -m py_compile *.py
```

Exercise the behavior you changed in the running application. In your pull
request, describe the resulting behavior and the checks you ran. See
[README.md](README.md) for configuration and feature details.
