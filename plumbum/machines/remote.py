import re
from contextlib import contextmanager
from plumbum.commands import CommandNotFound, shquote, ConcreteCommand
from plumbum.lib import _setdoc, ProcInfo, six
from plumbum.machines.local import LocalPath
from tempfile import NamedTemporaryFile
from plumbum.machines.base import BaseMachine
from plumbum.machines.env import BaseEnv
from plumbum.path.remote import RemotePath, RemoteWorkdir, StatRes, StatResTerse


class RemoteEnv(BaseEnv):
    """The remote machine's environment; exposes a dict-like interface"""

    __slots__ = ["_orig", "remote"]

    def __init__(self, remote):
        self.remote = remote
        session = remote._session
        # GNU env has a -0 argument; use it if present. Otherwise,
        # fall back to calling printenv on each (possible) variable
        # from plain env.
        env0 = session.run("env -0; echo")
        if env0[0] == 0 and not env0[2].rstrip():
            self._curr = dict(
                line.split('=', 1) for line in env0[1].split('\x00')
                if '=' in line)
        else:
            lines = session.run("env; echo")[1].splitlines()
            split = (line.split('=', 1) for line in lines)
            keys = (line[0] for line in split if len(line) > 1)

            # check whether printenv works - in busybox not always
            res = session.run('printenv "USER"; echo')
            cmd = 'echo "$' if  res[2] else 'printenv "'

            runs = ((key, session.run('%s%s"; echo' % (cmd, key)))
                    for key in keys)
            self._curr = dict(
                (key, run[1].rstrip('\n')) for (key, run) in runs
                if run[0] == 0 and run[1].rstrip('\n') and not run[2])
        self._orig = self._curr.copy()
        BaseEnv.__init__(self, self.remote.path, ":")

    @_setdoc(BaseEnv)
    def __delitem__(self, name):
        BaseEnv.__delitem__(self, name)
        self.remote._session.run("unset %s" % (name, ))

    @_setdoc(BaseEnv)
    def __setitem__(self, name, value):
        BaseEnv.__setitem__(self, name, value)
        self.remote._session.run("export %s=%s" % (name, shquote(value)))

    @_setdoc(BaseEnv)
    def pop(self, name, *default):
        BaseEnv.pop(self, name, *default)
        self.remote._session.run("unset %s" % (name, ))

    @_setdoc(BaseEnv)
    def update(self, *args, **kwargs):
        BaseEnv.update(self, *args, **kwargs)
        self.remote._session.run("export " + " ".join(
            "%s=%s" % (k, shquote(v)) for k, v in self.getdict().items()))

    def expand(self, expr):
        """Expands any environment variables and home shortcuts found in ``expr``
        (like ``os.path.expanduser`` combined with ``os.path.expandvars``)

        :param expr: An expression containing environment variables (as ``$FOO``) or
                     home shortcuts (as ``~/.bashrc``)

        :returns: The expanded string"""
        return self.remote.expand(expr)

    def expanduser(self, expr):
        """Expand home shortcuts (e.g., ``~/foo/bar`` or ``~john/foo/bar``)

        :param expr: An expression containing home shortcuts

        :returns: The expanded string"""
        return self.remote.expanduser(expr)

    # def clear(self):
    #    BaseEnv.clear(self, *args, **kwargs)
    #    self.remote._session.run("export %s" % " ".join("%s=%s" % (k, v) for k, v in self.getdict()))

    def getdelta(self):
        """Returns the difference between the this environment and the original environment of
        the remote machine"""
        self._curr["PATH"] = self.path.join()

        delta = {}
        for k, v in self._curr.items():
            if k not in self._orig:
                delta[k] = str(v)
        for k, v in self._orig.items():
            if k not in self._curr:
                delta[k] = ""
            else:
                if v != self._curr[k]:
                    delta[k] = self._curr[k]

        return delta


class RemoteCommand(ConcreteCommand):
    __slots__ = ["remote", "executable"]
    QUOTE_LEVEL = 1

    def __init__(self, remote, executable, encoding="auto"):
        self.remote = remote
        ConcreteCommand.__init__(
            self, executable, remote.custom_encoding
            if encoding == "auto" else encoding)

    @property
    def machine(self):
        return self.remote

    def __repr__(self):
        return "RemoteCommand(%r, %r)" % (self.remote, self.executable)

    def popen(self, args=(), **kwargs):
        return self.remote.popen(self[args], **kwargs)

    def nohup(self, cwd='.', stdout='nohup.out', stderr=None, append=True):
        """Runs a command detached."""
        return self.machine.daemonic_popen(self, cwd, stdout, stderr, append)


class ClosedRemoteMachine(Exception):
    pass


class ClosedRemote(object):
    __slots__ = ["_obj", "__weakref__"]

    def __init__(self, obj):
        self._obj = obj

    def close(self):
        pass

    def __getattr__(self, name):
        raise ClosedRemoteMachine("%r has been closed" % (self._obj, ))


class BaseRemoteMachine(BaseMachine):
    """Represents a *remote machine*; serves as an entry point to everything related to that
    remote machine, such as working directory and environment manipulation, command creation,
    etc.

    Attributes:

    * ``cwd`` - the remote working directory
    * ``env`` - the remote environment
    * ``custom_encoding`` - the remote machine's default encoding (assumed to be UTF8)
    * ``connect_timeout`` - the connection timeout


    There also is a _cwd attribute that exists if the cwd is not current (del if cwd is changed).
    """

    # allow inheritors to override the RemoteCommand class
    RemoteCommand = RemoteCommand

    @property
    def cwd(self):
        if not hasattr(self, '_cwd'):
            self._cwd = RemoteWorkdir(self)
        return self._cwd

    def __init__(self, encoding="utf8", connect_timeout=10, new_session=False):
        self.custom_encoding = encoding
        self.connect_timeout = connect_timeout
        self._session = self.session(new_session=new_session)
        self.uname = self._get_uname()
        self.env = RemoteEnv(self)
        self._python = None

    def _get_uname(self):
        rc, out, _ = self._session.run("uname", retcode=None)
        if rc == 0:
            return out.strip()
        else:
            rc, out, _ = self._session.run(
                "python -c 'import platform;print(platform.uname()[0])'",
                retcode=None)
            if rc == 0:
                return out.strip()
            else:
                # all POSIX systems should have uname. make an educated guess it's Windows
                return "Windows"

    def __repr__(self):
        return "<%s %s>" % (self.__class__.__name__, self)

    def __enter__(self):
        return self

    def __exit__(self, t, v, tb):
        self.close()

    def close(self):
        """closes the connection to the remote machine; all paths and programs will
        become defunct"""
        self._session.close()
        self._session = ClosedRemote(self)

    def path(self, *parts):
        """A factory for :class:`RemotePaths <plumbum.path.remote.RemotePath>`.
        Usage: ``p = rem.path("/usr", "lib", "python2.7")``
        """
        parts2 = [str(self.cwd)]
        for p in parts:
            if isinstance(p, LocalPath):
                raise TypeError("Cannot construct RemotePath from %r" % (p, ))
            parts2.append(self.expanduser(str(p)))
        return RemotePath(self, *parts2)

    def which(self, progname):
        """Looks up a program in the ``PATH``. If the program is not found, raises
        :class:`CommandNotFound <plumbum.commands.CommandNotFound>`

        :param progname: The program's name. Note that if underscores (``_``) are present
                         in the name, and the exact name is not found, they will be replaced
                         in turn by hyphens (``-``) then periods (``.``), and the name will
                         be looked up again for each alternative

        :returns: A :class:`RemotePath <plumbum.path.local.RemotePath>`
        """
        alternatives = [progname]
        if "_" in progname:
            alternatives.append(progname.replace("_", "-"))
            alternatives.append(progname.replace("_", "."))
        for name in alternatives:
            for p in self.env.path:
                fn = p / name
                if fn.access("x") and not fn.is_dir():
                    return fn

        raise CommandNotFound(progname, self.env.path)

    def __getitem__(self, cmd):
        """Returns a `Command` object representing the given program. ``cmd`` can be a string or
        a :class:`RemotePath <plumbum.path.remote.RemotePath>`; if it is a path, a command
        representing this path will be returned; otherwise, the program name will be looked up in
        the system's ``PATH`` (using ``which``). Usage::

            r_ls = rem["ls"]
        """
        if isinstance(cmd, RemotePath):
            if cmd.remote is self:
                return self.RemoteCommand(self, cmd)
            else:
                raise TypeError(
                    "Given path does not belong to this remote machine: %r" %
                    (cmd, ))
        elif not isinstance(cmd, LocalPath):
            if "/" in cmd or "\\" in cmd:
                return self.RemoteCommand(self, self.path(cmd))
            else:
                return self.RemoteCommand(self, self.which(cmd))
        else:
            raise TypeError("cmd must not be a LocalPath: %r" % (cmd, ))

    @property
    def python(self):
        """A command that represents the default remote python interpreter"""
        if not self._python:
            self._python = self["python"]
        return self._python

    def session(self, isatty=False, new_session=False):
        """Creates a new :class:`ShellSession <plumbum.session.ShellSession>` object; this invokes the user's
        shell on the remote machine and executes commands on it over stdin/stdout/stderr"""
        raise NotImplementedError()

    def download(self, src, dst):
        """Downloads a remote file/directory (``src``) to a local destination (``dst``).
        ``src`` must be a string or a :class:`RemotePath <plumbum.path.remote.RemotePath>`
        pointing to this remote machine, and ``dst`` must be a string or a
        :class:`LocalPath <plumbum.machines.local.LocalPath>`"""
        raise NotImplementedError()

    def upload(self, src, dst):
        """Uploads a local file/directory (``src``) to a remote destination (``dst``).
        ``src`` must be a string or a :class:`LocalPath <plumbum.machines.local.LocalPath>`,
        and ``dst`` must be a string or a :class:`RemotePath <plumbum.path.remote.RemotePath>`
        pointing to this remote machine"""
        raise NotImplementedError()

    def popen(self, args, **kwargs):
        """Spawns the given command on the remote machine, returning a ``Popen``-like object;
        do not use this method directly, unless you need "low-level" control on the remote
        process"""
        raise NotImplementedError()

    def list_processes(self):
        """
        Returns information about all running processes (on POSIX systems: using ``ps``)

        .. versionadded:: 1.3
        """
        ps = self["ps"]
        lines = ps("-e", "-o", "pid,uid,stat,args").splitlines()
        lines.pop(0)  # header
        for line in lines:
            parts = line.strip().split()
            yield ProcInfo(
                int(parts[0]), int(parts[1]), parts[2], " ".join(parts[3:]))

    def pgrep(self, pattern):
        """
        Process grep: return information about all processes whose command-line args match the given regex pattern
        """
        pat = re.compile(pattern)
        for procinfo in self.list_processes():
            if pat.search(procinfo.args):
                yield procinfo

    @contextmanager
    def tempdir(self):
        """A context manager that creates a remote temporary directory, which is removed when
        the context exits"""
        _, out, _ = self._session.run("mktemp -d tmp.XXXXXXXXXX")
        dir = self.path(out.strip())  # @ReservedAssignment
        try:
            yield dir
        finally:
            dir.delete()

    #
    # Path implementation
    #
    def _path_listdir(self, fn):
        files = self._session.run("ls -a %s" % (shquote(fn), ))[1].splitlines()
        files.remove(".")
        files.remove("..")
        return files

    def _path_glob(self, fn, pattern):
        # shquote does not work here due to the way bash loops use space as a seperator
        pattern = pattern.replace(" ", r"\ ")
        fn = fn.replace(" ", r"\ ")
        matches = self._session.run(
            r'for fn in {0}/{1}; do echo $fn; done'.format(
                fn, pattern))[1].splitlines()
        if len(matches) == 1 and not self._path_stat(matches[0]):
            return []  # pattern expansion failed
        return matches

    def _path_getuid(self, fn):
        stat_cmd = "stat -c '%u,%U' " if self.uname not in (
            'Darwin', 'FreeBSD') else "stat -f '%u,%Su' "
        return self._session.run(stat_cmd + shquote(fn))[1].strip().split(",")

    def _path_getgid(self, fn):
        stat_cmd = "stat -c '%g,%G' " if self.uname not in (
            'Darwin', 'FreeBSD') else "stat -f '%g,%Sg' "
        return self._session.run(stat_cmd + shquote(fn))[1].strip().split(",")

    def _path_stat(self, fn):
        if self.uname not in ('Darwin', 'FreeBSD'):
            stat_cmd = "stat -c '%F,%f,%i,%d,%h,%u,%g,%s,%X,%Y,%Z' "
        else:
            stat_cmd = "stat -f '%HT,%Xp,%i,%d,%l,%u,%g,%z,%a,%m,%c' "
        rc, out, _ = self._session.run(stat_cmd + shquote(fn), retcode=None)

        # some debug help to determine whether stat command is OK
        #print("uname: %s\nCmd: %s\nRes:\n%s" % (self.uname,stat_cmd+shquote(fn), out))

        if rc != 0:
            # attempt terse format if -c is not supported on busybox
            stat_cmd = "stat -t "
            rc, out, _ = self._session.run(stat_cmd + shquote(fn), retcode=None)
            if rc != 0:
                return None
            else:
                statres = out.strip().split(" ")
                fname = statres.pop(0).lower()
                # mode
                statres[2] = int(statres[2], 16)
                statres[5] = 0
                #print statres
                res = StatResTerse(tuple(int(sr) for sr in statres))
                # Important!
                # repeat stat in a different format as our terse stat without normal format
                # option compiled in reports all files with 'd' regardless of whether it is a dir or not:
                # without format compiled in:
                #  ~ # stat /bin/ps
                #  File: '/bin/ps'
                #  Size: 71816           Blocks: 144        IO Block: 4096   regular file
                # good, but
                #  ~ # stat -t /bin/ps
                #  /bin/ps 71816 144 81fd 0 0 d 2490475 1 0 0 1507722269 1507722269 1507722269 4096
                # with format compiled in (notice the '0 0 b' part instead of '0 0 d'):
                #  ~ # stat -t /bin/ps
                #  /bin/ps 71772 144 81fd 0 0 b 4980955 1 0 0 1508858360 1508854706 1508854706 4096
                # (May not be because of the missing -c flag, one was on MDR HS, other on MDR, compiled on different PC.)
                stat_cmd = "stat "
                rc, out, _ = self._session.run(stat_cmd + shquote(fn), retcode=None)
                lines = out.split("\n")
                broken_line = lines[1].split("  ")
                text_mode = broken_line[-1]
                res.text_mode = text_mode
        else:
            statres = out.strip().split(",")
            text_mode = statres.pop(0).lower()
            res = StatRes((int(statres[0], 16),) + tuple(
                int(sr) for sr in statres[1:]))
            res.text_mode = text_mode
        return res

    def _path_delete(self, fn):
        self._session.run("rm -rf %s" % (shquote(fn), ))

    def _path_move(self, src, dst):
        self._session.run("mv %s %s" % (shquote(src), shquote(dst)))

    def _path_copy(self, src, dst):
        self._session.run("cp -r %s %s" % (shquote(src), shquote(dst)))

    def _path_mkdir(self, fn, mode=None, minus_p=True):
        p_str = "-p " if minus_p else ""
        cmd = "mkdir %s%s" % (p_str, shquote(fn))
        self._session.run(cmd)

    def _path_chmod(self, mode, fn):
        self._session.run("chmod %o %s" % (mode, shquote(fn)))

    def _path_touch(self, path):
        self._session.run("touch {path}".format(path=path))

    def _path_chown(self, fn, owner, group, recursive):
        args = ["chown"]
        if recursive:
            args.append("-R")
        if owner is not None and group is not None:
            args.append("%s:%s" % (owner, group))
        elif owner is not None:
            args.append(str(owner))
        elif group is not None:
            args.append(":%s" % (group, ))
        args.append(shquote(fn))
        self._session.run(" ".join(args))

    def _path_read(self, fn):
        data = self["cat"](fn)
        if self.custom_encoding and isinstance(data, six.unicode_type):
            data = data.encode(self.custom_encoding)
        return data

    def _path_write(self, fn, data):
        if self.custom_encoding and isinstance(data, six.unicode_type):
            data = data.encode(self.custom_encoding)
        with NamedTemporaryFile() as f:
            f.write(data)
            f.flush()
            f.seek(0)
            self.upload(f.name, fn)

    def _path_link(self, src, dst, symlink):
        self._session.run(
            "ln %s %s %s" % ("-s"
                             if symlink else "", shquote(src), shquote(dst)))

    @_setdoc(BaseEnv)
    def expand(self, expr):
        return self._session.run("echo %s" % (expr, ))[1].strip()

    @_setdoc(BaseEnv)
    def expanduser(self, expr):
        if not any(part.startswith("~") for part in expr.split("/")):
            return expr
        # we escape all $ signs to avoid expanding env-vars
        return self._session.run(
            "echo %s" % (expr.replace("$", "\\$"), ))[1].strip()
