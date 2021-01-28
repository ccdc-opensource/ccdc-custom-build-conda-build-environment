"""Create a miniconda environment containing the conda-build package that is necessary to build the API packages.
"""
import glob
import os
import platform
import requests
import shutil
import subprocess
import sys
import tempfile
import re
from pathlib import Path

def macos():
    return sys.platform == 'darwin'

def windows():
    return sys.platform == 'win32'

def linux():
    return sys.platform.startswith('linux')

if windows():
    # Add functionality to restore the environment after miniconda installer has messed around with it
    import ctypes
    from ctypes import wintypes
    import winreg as reg

    HWND_BROADCAST = 0xffff
    WM_SETTINGCHANGE = 0x001A
    SMTO_ABORTIFHUNG = 0x0002
    SendMessageTimeout = ctypes.windll.user32.SendMessageTimeoutW
    SendMessageTimeout.restype = None #wintypes.LRESULT
    SendMessageTimeout.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                wintypes.LPCWSTR, wintypes.UINT, wintypes.UINT, ctypes.POINTER(wintypes.DWORD)]

    def sz_expand(value, value_type):
        if value_type == reg.REG_EXPAND_SZ:
            return reg.ExpandEnvironmentStrings(value)
        else:
            return value

    def remove_from_system_path(pathname, allusers=True, path_env_var='PATH'):
        r"""Removes all entries from the path which match the value in 'pathname'

        You must call broadcast_environment_settings_change() after you are finished
        manipulating the environment with this and other functions.

        For example,
            # Remove Anaconda from PATH
            remove_from_system_path(r'C:\Anaconda')
            broadcast_environment_settings_change()
        """
        pathname = os.path.normcase(os.path.normpath(pathname))

        envkeys = [(reg.HKEY_CURRENT_USER, r'Environment')]
        if allusers:
            envkeys.append((reg.HKEY_LOCAL_MACHINE,
                r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'))
        for root, keyname in envkeys:
            key = reg.OpenKey(root, keyname, 0,
                    reg.KEY_QUERY_VALUE|reg.KEY_SET_VALUE)
            reg_value = None
            try:
                reg_value = reg.QueryValueEx(key, path_env_var)
            except WindowsError:
                # This will happen if we're a non-admin install and the user has
                # no PATH variable.
                reg.CloseKey(key)
                continue

            try:
                any_change = False
                results = []
                for v in reg_value[0].split(os.pathsep):
                    vexp = sz_expand(v, reg_value[1])
                    # Check if the expanded path matches the
                    # requested path in a normalized way
                    if os.path.normcase(os.path.normpath(vexp)) == pathname:
                        any_change = True
                    else:
                        # Append the original unexpanded version to the results
                        results.append(v)

                modified_path = os.pathsep.join(results)
                if any_change:
                    reg.SetValueEx(key, path_env_var, 0, reg_value[1], modified_path)
            except:
                # If there's an error (e.g. when there is no PATH for the current
                # user), continue on to try the next root/keyname pair
                reg.CloseKey(key)

    def add_to_system_path(paths, allusers=True, path_env_var='PATH'):
        """Adds the requested paths to the system PATH variable.

        You must call broadcast_environment_settings_change() after you are finished
        manipulating the environment with this and other functions.

        """
        # Make sure it's a list
        if not issubclass(type(paths), list):
            paths = [paths]

        # Ensure all the paths are valid before we start messing with the
        # registry.
        new_paths = None
        for p in paths:
            p = os.path.abspath(p)
            if not os.path.isdir(p):
                raise RuntimeError(
                    'Directory "%s" does not exist, '
                    'cannot add it to the path' % p
                )
            if new_paths:
                new_paths = new_paths + os.pathsep + p
            else:
                new_paths = p

        if allusers:
            # All Users
            root, keyname = (reg.HKEY_LOCAL_MACHINE,
                r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment')
        else:
            # Just Me
            root, keyname = (reg.HKEY_CURRENT_USER, r'Environment')

        key = reg.OpenKey(root, keyname, 0,
                reg.KEY_QUERY_VALUE|reg.KEY_SET_VALUE)

        reg_type = None
        reg_value = None
        try:
            try:
                reg_value = reg.QueryValueEx(key, path_env_var)
            except WindowsError:
                # This will happen if we're a non-admin install and the user has
                # no PATH variable; in which case, we can write our new paths
                # directly.
                reg_type = reg.REG_EXPAND_SZ
                final_value = new_paths
            else:
                reg_type = reg_value[1]
                # If we're an admin install, put us at the end of PATH.  If we're
                # a user install, throw caution to the wind and put us at the
                # start.  (This ensures we're picked up as the default python out
                # of the box, regardless of whether or not the user has other
                # pythons lying around on their PATH, which would complicate
                # things.  It's also the same behavior used on *NIX.)
                if allusers:
                    final_value = reg_value[0] + os.pathsep + new_paths
                else:
                    final_value = new_paths + os.pathsep + reg_value[0]

            reg.SetValueEx(key, path_env_var, 0, reg_type, final_value)

        finally:
            reg.CloseKey(key)

    def broadcast_environment_settings_change():
        """Broadcasts to the system indicating that master environment variables have changed.

        This must be called after using the other functions in this module to
        manipulate environment variables.
        """
        SendMessageTimeout(HWND_BROADCAST, WM_SETTINGCHANGE, 0, u'Environment',
                    SMTO_ABORTIFHUNG, 5000, ctypes.pointer(wintypes.DWORD()))

package_name = 'conda_buildenv'

class MinicondaBuildEnvironment:
    def __init__(self):
        self.required_conda_packages = [
            'conda-build',
            'sphinx',
        ]
        self.extensions = {
            'Windows': 'exe',
            'Linux': 'sh',
            'Darwin': 'sh'
        }
        self.platforms = {
            'Windows': 'Windows',
            'Linux': 'Linux',
            'Darwin': 'MacOSX'
        }
        self.architectures = {
            '64bit': 'x86_64'
        }
        self.system = platform.system()
        self.conda_python_version = '3'
        self.bitness = '64bit'
        self.distribution = 'Miniconda'
        self.conda_bz2_src_packages = os.path.join(self.conda_buildenv_versioned_destdir(), 'pkgs', '*.bz2')
        self.conda_conda_src_packages = os.path.join(self.conda_buildenv_versioned_destdir(), 'pkgs', '*.conda')

    # Pass the required miniconda installer version from devops pipelines variables
    def miniconda_installer_version(self):
        return os.environ.get('MINICONDA_INSTALLER_VERSION', 'py37_4.9.2')

    # Pass the build id from devops pipelines variables
    # Make sure the resulting artefact is clearly labeled if produced on a developer machine
    def build_id(self):
        return os.environ.get('BUILD_BUILDID', 'DEVELOPER_VERSION')

    # Pass the operating system name from devops pipelines variables
    # Make sure the resulting artefact is clearly labeled if produced on a developer machine
    def build_osname(self):
        return os.environ.get('BUILDOSNAME', 'for_my_developer_os')

    def output_base_name(self):
        components = [
            package_name,
            self.miniconda_installer_version(),
            self.build_id(),
            self.build_osname(),
        ]
        return '-'.join(components)

    def conda_buildenv_destdir(self):
        if windows():
            return Path('D:\\x_mirror\\buildman\\tools\\conda_buildenv')
        else:
            return Path('/opt/ccdc/third-party/conda_buildenv')

    def conda_buildenv_versioned_destdir(self):
        return self.conda_buildenv_destdir() / self.output_base_name()

    def prepare_conda_buildenv_versioned_destdir(self):
        if linux() or macos():
            subprocess.run(f'sudo mkdir -p {self.conda_buildenv_destdir()}', shell=True)
            subprocess.run(f'sudo chown $USER {self.conda_buildenv_destdir()}', shell=True)
        os.makedirs(self.conda_buildenv_versioned_destdir())

    @property
    def build_temp(self):
        '''Where temporary files are stored'''
        return Path('build_temp')
    
    @property
    def local_miniconda_installer_file(self):
        '''local path to the miniconda installer'''
        return os.path.join(self.build_temp, self.installer_name)

    def output_archive_filename(self):
            return f'{self.output_base_name()}.tar.gz'

    @property
    def installer_name(self):
        # (Ana|Mini)conda-<VERSION>-<PLATFORM>-<ARCHITECTURE>.<EXTENSION>
        return '{0}{1}-{2}-{3}-{4}.{5}'.format(
            self.distribution,
            self.conda_python_version,
            self.miniconda_installer_version(),
            self.platforms[self.system],
            self.architectures[self.bitness],
            self.extensions[self.system])

    def fetch_miniconda_installer(self):
        installer_url='https://repo.continuum.io/miniconda/%s' % self.installer_name
        print("Get %s -> %s" % (installer_url, self.local_miniconda_installer_file))
        r = requests.get(installer_url)
        with open(self.local_miniconda_installer_file, 'wb') as fd:
            for chunk in r.iter_content(chunk_size=128):
                fd.write(chunk)

    def clean_destdir(self):
        try:
            shutil.rmtree(self.conda_buildenv_versioned_destdir())
        except:
            pass

    def clean_build_temp(self):
        try:
            shutil.rmtree(self.build_temp)
        except:
            pass

    def conda_cleanup(self, *package_specs):
        """Remove package archives (so that we don't distribute them as they are already part of the installer)
        """
        self._run_pkg_manager('conda', ['clean', '-y', '--all'])

    def conda_update(self, *package_specs):
        """Update local packages that are part of the installer
        """
        self._run_pkg_manager('conda', ['update', '-y', '--all'])

    def package_name(self, package_filename):
        """Return the bit of a filename before the version number starts
        """
        return re.match(r"(.*)-\d.*", package_filename).group(1)

    def install_miniconda(self):
        print('Running %s' % self.install_args)
        outcome = subprocess.call(self.install_args)

        if windows():
            self._clean_up_system_path()

        if outcome != 0:
            raise RuntimeError('Failed to run "{0}"'.format(self.install_args))

    @property
    def install_args(self):
        if windows():
            install_args = [self.local_miniconda_installer_file,
                            '/S',     # run install in batch mode (without manual intervention)
                            '/D=' + os.path.abspath(self.conda_buildenv_versioned_destdir())]
        else:
            install_args = ['sh',
                            self.local_miniconda_installer_file,
                            '-b',     # run install in batch mode (without manual intervention)
                            '-f',     # no error if install prefix already exists
                            '-p', os.path.abspath(self.conda_buildenv_versioned_destdir())]
        return install_args

    def _clean_up_system_path(self):
        """The Windows installer modifies the PATH env var, so let's
        revert that using the same mechanism.
        """
        for_all_users = (not os.path.exists(
            os.path.join(self.conda_buildenv_versioned_destdir(), '.nonadmin')))

        remove_from_system_path(self.conda_buildenv_versioned_destdir(),
                                for_all_users,
                                'PATH')
        remove_from_system_path(os.path.join(self.conda_buildenv_versioned_destdir(), 'Scripts'),
                                for_all_users,
                                'PATH')
        broadcast_environment_settings_change()

    def conda_install(self, *package_specs):
        """Install a conda package given its specifications.
        E.g. self.conda_install('numpy==1.9.2', 'lxml')
        """
        self._run_pkg_manager('conda', ['install', '-y'], *package_specs)

    def _run_pkg_manager(self, pkg_manager_name, extra_args, *package_specs):
        my_env = os.environ.copy()
        # Set the condarc to the channels we want
        my_env["CONDARC"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'condarc-for-offline-installer-creation')
        # add Library\bin to path so that conda can find libcrypto
        if windows():
            my_env['PATH'] = "%s;%s" % (os.path.join(self.conda_buildenv_versioned_destdir(), 'Library', 'bin'), my_env['PATH'])
        args = [self._args_for(pkg_manager_name)] + extra_args + list(package_specs)
        outcome = subprocess.call(args, env=my_env)
        if outcome != 0:
            print('_run_pkg_manager fail info')
            print(args)
            print(my_env)
            raise RuntimeError('Could not install {0} with {1}'.format(' '.join(package_specs), pkg_manager_name))

    def _args_for(self, executable_name):
        return os.path.join(self.conda_buildenv_versioned_destdir(),
                            ('Scripts' if windows() else 'bin'),
                            executable_name + ('.exe' if windows() else ''))

    def check_condarc_presence(self):
        for path in [
            '/etc/conda/.condarc',
            '/etc/conda/condarc',
            '/etc/conda/condarc.d/',
            '/var/lib/conda/.condarc',
            '/var/lib/conda/condarc',
            '/var/lib/conda/condarc.d/',
            '~/.conda/.condarc',
            '~/.conda/condarc',
            '~/.conda/condarc.d/',
            '~/.condarc',
            ]:
            if os.path.exists(os.path.expanduser(path)):
                print('Conda configuration found in %s. This might affect installation of packages' % path)

    def install(self):
        print('Cleaning up destination and temporary build directories')
        self.clean_destdir()
        self.prepare_conda_buildenv_versioned_destdir()
        self.clean_build_temp()
        os.makedirs(self.build_temp)

        print('Getting installer')
        self.fetch_miniconda_installer()

        print('Check there are no condarc files around')
        self.check_condarc_presence()

        print('Install miniconda in the destdir directory')
        self.install_miniconda()

        print('Download updates so that we can distribute them consistently')
        self.conda_update()

        print('Fetch packages')
        self.conda_install(*self.required_conda_packages)

        print('Remove conda package files to reduce size')
        self.conda_cleanup()

    def create_archive(self):
        if 'BUILD_ARTIFACTSTAGINGDIRECTORY' in os.environ:
            archive_output_directory = Path(
                os.environ['BUILD_ARTIFACTSTAGINGDIRECTORY'])
        else:
            archive_output_directory = self.build_temp
        archive_output_directory.mkdir(parents=True, exist_ok=True)
        output_file_absolute = archive_output_directory.resolve() / self.output_archive_filename()
        print(f'Creating {output_file_absolute}')
        command = [
            'tar',
            '-zcf',
            f'{ output_file_absolute }',  # the tar filename
            f'{ self.conda_buildenv_versioned_destdir().relative_to(self.conda_buildenv_destdir()) }',
        ]

        try:
            # keep the name + version directory in the archive, but not the package name directory
            subprocess.run(command, check=True, cwd=self.conda_buildenv_destdir())
        except subprocess.CalledProcessError as e:
            if not windows():
                raise e
            command.insert(1, '--force-local')
            # keep the name + version directory in the archive, but not the package name directory
            subprocess.run(command, check=True, cwd=self.conda_buildenv_destdir())

if __name__ == '__main__':
    MinicondaBuildEnvironment().install()
    MinicondaBuildEnvironment().create_archive()


