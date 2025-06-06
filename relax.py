# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Structure refinement"""
import os.path

from simtk.openmm.app import *
from simtk.openmm import *
from simtk.unit import *
import simtk.openmm as mm
from sys import stdout
from rhofold.utils import timing, tmpdir
import subprocess
import shutil
import torch

def get_gpu_device_indexes():
    if not torch.cuda.is_available():
        return "'DeviceIndex': 'None'"
    
    device_count = torch.cuda.device_count()
    indexes = ",".join(str(i) for i in range(device_count))
    return f"'DeviceIndex': '{indexes}'"

class AmberRelaxation(object):
    """Amber relaxation."""
    def __init__(
        self,
        *,
        max_iterations: int,
        use_gpu: bool = False,
        imp_solv: bool = False,
        logger,
    ):
        """Initialize Amber Relaxer.

        Args:
          max_iterations:
        """
        self._max_iterations = max_iterations
        self._use_gpu = use_gpu
        self.logger = logger
        self.imp_solv = imp_solv

        if self._use_gpu:
            self.platform = mm.Platform.getPlatformByName('CUDA')
            self.logger.info("    AmberRelaxation: Using GPU")
        else:
            try:
                self.platform = mm.Platform.getPlatformByName('OpenCL')
                self.logger.info("    AmberRelaxation: Using OpenCL")
            except:
                self.platform = None
                self.logger.info("    AmberRelaxation: Using CPU")

    def process( self, pdbin, pdbout):
        """Runs Amber relax on a prediction, adds hydrogens, returns PDB string."""

        with tmpdir(base_dir=f'{os.path.dirname(pdbout)}') as tmp_dir:

            pdbin_tmp = os.path.join(tmp_dir, os.path.basename(pdbin))
            pdbout_tmp = os.path.join(tmp_dir, os.path.basename(pdbout))

            self._rewrite_pdb(pdbin, pdbin_tmp)
            self._run_amber_relax(pdbin_tmp,  pdbout_tmp)
            self._rewrite_pdb_rm_H(pdbout_tmp, pdbout)

            self.logger.info('    Export PDB file to %s' % pdbout)


    def _run_amber_relax(self, pdbin, pdbout):
        '''
        Run AMBER relaxation
        '''

        pdb = PDBFile(pdbin)
        modeller = Modeller(pdb.topology, pdb.positions)
        if self.imp_solv:
            forcefield = ForceField('amber14-all.xml', 'implicit/gbn2.xml') # load implicit solvent forcefield
            modeller.addHydrogens(forcefield)
        else:
            forcefield = ForceField('amber14-all.xml', 'amber14/tip3pfb.xml') # No any alteratiion
            modeller.addHydrogens(forcefield)
            modeller.addSolvent(forcefield, padding=1 * nanometer)

        system = forcefield.createSystem(
            modeller.topology,
            nonbondedMethod=NoCutoff,
            nonbondedCutoff=1 * nanometer,
            constraints=HBonds
        )
        #modeller.addHydrogens(forcefield)
        #modeller.addSolvent(forcefield, padding=1 * nanometer)

        #system = forcefield.createSystem(modeller.topology, nonbondedMethod=NoCutoff, nonbondedCutoff=1 * nanometer,
        #                                 constraints=HBonds)

        integrator = LangevinIntegrator(300 * kelvin, 1 / picosecond, 0.002 * picoseconds)
        properties = {
            'DeviceIndex': get_gpu_device_indexes()  # Use Multiple GPUs,
            #'Precision': 'half'      # Use NVIDIA amp optimizations or not
        }
        simulation = Simulation(modeller.topology, system, integrator, self.platform)
        simulation.context.setPositions(modeller.positions)
        simulation.reporters.append(StateDataReporter(stdout, 1000, step=True, potentialEnergy=True, temperature=True))
        self.logger.info("    Minimizing ...")

        simulation.minimizeEnergy(maxIterations=self._max_iterations)
        position = simulation.context.getState(getPositions=True).getPositions()
        energy = simulation.context.getState(getEnergy=True).getPotentialEnergy()
        app.PDBFile.writeFile(simulation.topology, position, open(pdbout, 'w'))
        self.logger.info('    Energy at Minima is %3.3f kcal/mol' % (energy._value * KcalPerKJ))


    def _rewrite_pdb(self, inp_fpath, out_fpath):
        tofile = open(out_fpath, 'w')

        with open(inp_fpath) as lines:
            lines = lines.readlines()

            resinidexs = []
            for l in lines:
                if len(l.split()) > 5 and l.split()[0] == "ATOM":
                    resindx_int = int(l.split()[5])
                    resinidexs.append(resindx_int)

            resinidexs.sort()
            for l in lines:
                if len(l.split()) > 5 and l.split()[0] == "ATOM":
                    atomn = l.split()[2]
                    resindx_int = int(l.split()[5])

                    l = list(l)
                    if resindx_int == resinidexs[0]:
                        l[18:20] = l[19:20] + ['5']
                    elif resindx_int == resinidexs[-1]:
                        l[18:20] = l[19:20] + ['3']
                    nl = ''.join(l)
                    if not ("P" in atomn and resindx_int == 1):
                        tofile.write(nl)
        tofile.close()

    def _rewrite_pdb_rm_H(self, inp_fpath, out_fpath):
        tofile = open(out_fpath, 'w')

        with open(inp_fpath) as lines:
            for l in lines:
                if len(l.split()) > 5 and l.split()[0] == "ATOM":
                    atomn = l.split()[2]
                    if 'H' in atomn:
                        continue
                    tofile.write(l)

        tofile.close()

class QRNASRelaxation(object):
    """Amber relaxation."""
    def __init__(
        self,
        *,
        binary_path: str,
        forcefield_path: str,
        max_iterations: int,
        logger,
    ):
        """Initialize QRNAS Relaxer.

        Args:
            binary_path: The path to the QRNAS executable.
            forcefield_path: The path to the QRNAS forcefield_path.
        """

        self.binary_path = binary_path
        self._max_iterations = max_iterations
        self.logger = logger
        os.environ["QRNAS_FF_DIR"] = forcefield_path


    def process( self, pdbin, pdbout, is_fix = True):
        """Runs QRNAS relax on a prediction."""

        with tmpdir(base_dir=f'{os.path.dirname(pdbout)}') as tmp_dir:

            config = os.path.join(tmp_dir, 'configfile.txt')

            with open(config, 'w') as f:
                f.write(f'WRITEFREQ  1000\n')
                f.write(f'NSTEPS     {self._max_iterations}\n')
                f.write(f'NUMTHREADS 16\n')

            pdbin_tmp = os.path.join(tmp_dir, os.path.basename(pdbin))
            pdbout_tmp = os.path.join(tmp_dir, os.path.basename(pdbout))
            self._rewrite_pdb_occupancy(pdbin, pdbin_tmp, is_fix)

            cmd = [
                self.binary_path,
                '-P', '-i', pdbin_tmp,
                '-o', pdbout_tmp,
                '-c', config
            ]

            self.logger.info('Launching subprocess "%s"', ' '.join(cmd))
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            with timing(f'QRNAS iterations: {self._max_iterations}', logger=self.logger):
                stdout, stderr = process.communicate()
                retcode = process.wait()

            if retcode:
                # Logs have a 15k character limit, so log QRNAS error line by line.
                self.logger.error('QRNAS failed. QRNAS stderr begin:')
                for error_line in stderr.decode('utf-8').splitlines():
                    if error_line.strip():
                        self.logger.error(error_line.strip())
                self.logger.error('QRNAS stderr end')
                raise RuntimeError('QRNAS failed\nstdout:\n%s\n\nstderr:\n%s\n' % (
                    stdout.decode('utf-8'), stderr[:500_000].decode('utf-8')))

            self._rewrite_pdb_rm_H(pdbout_tmp, pdbout)

            self.logger.info('Export PDB file to %s' % pdbout)

    def _rewrite_pdb_occupancy(self, inp_fpath, out_fpath, is_fix = True):
        """

        Rewrite PDB occupancy for fixing atom in QRNAS refinement

        QRNAS is able to restrain the positions of specified atoms.
        The two alternatives are either “freezing” orand “pinning down” individual atoms.
        These restraints can be implemented by altering the occupancy and B-factor column in the input pdb file.
        If the occupancy of an atom is set equal to 0.00, its position is fixed / frozen, which means that will not be
        changed during the optimization. If the occupancy is set between 0.00 and 1.00 , the residue is “pinned down” to
        its original position, and the B-factor value is treated as a radius of unrestricted movement from the starting
        position. If the occupancy is set equal to 1.00, then the movement of the atom is not restricted
        (unless it is specified by other restraints).

        """

        tofile = open(out_fpath, 'w')

        with open(inp_fpath) as lines:
            lines = lines.readlines()

            resinidexs = []
            for l in lines:
                if len(l.split()) > 5 and l.split()[0] == "ATOM":
                    resindx_int = int(l.split()[5])
                    resinidexs.append(resindx_int)

            resinidexs.sort()
            for l in lines:
                if len(l.split()) > 5 and l.split()[0] == "ATOM":
                    atomn = l.split()[2]
                    resindx_int = int(l.split()[5])
                    l = list(l)

                    # fixed C1' atom
                    if is_fix and "C1'" in atomn:
                        l[56:60] = list('0.00')

                    nl = ''.join(l)
                    if not ("P" in atomn and resindx_int == 1):
                        tofile.write(nl)

        tofile.close()

    def _rewrite_pdb_rm_H(self, inp_fpath, out_fpath):
        tofile = open(out_fpath, 'w')

        with open(inp_fpath) as lines:
            for l in lines:
                if len(l.split()) > 5 and l.split()[0] == "ATOM":
                    atomn = l.split()[2]
                    if 'H' in atomn:
                        continue
                    tofile.write(l)

        tofile.close()

class BRIQRelaxation(object):
    """Amber relaxation."""
    def __init__(
        self,
        *,
        binary_dpath: str,
        forcefield_path: str,
        random_seed: int,
        logger,
    ):
        """Initialize BRIQ Relaxer.

        Args:
            binary_path: The path to the BRIQ executable.
            forcefield_path: The path to the BRIQ forcefield_path.
        """

        self.binary_dpath = binary_dpath
        os.environ["BRiQ_DATAPATH"] = forcefield_path
        self.random_seed = random_seed
        self.logger = logger

    def process(self, pdbin, pdbout, BRIQ_input = None, fix_non_paring_region = False):
        """Runs BRIQ relax on a prediction"""

        with tmpdir(base_dir=f'{os.path.dirname(pdbout)}') as tmp_dir:
            pdbin_tmp = os.path.join(tmp_dir, os.path.basename(pdbin))
            pdbout_tmp = os.path.join(tmp_dir, os.path.basename(pdbout))

            shutil.copyfile(pdbin, pdbin_tmp)

            if BRIQ_input is None:
                ss_tmp = pdbin_tmp.replace('.pdb', '.ss')
                cmd = [
                    f'{self.binary_dpath}/BRiQ_AssignSS',
                    pdbin_tmp,
                    ss_tmp
                ]

                # Assign SS based on input PDB
                self.logger.info('Launching subprocess "%s"', ' '.join(cmd))
                process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                stdout, stderr = process.communicate()
                retcode = process.wait()

                if retcode:
                    self.logger.error('BRIQ_AssignSS failed. BRIQ_AssignSS stderr begin:')
                    for error_line in stderr.decode('utf-8').splitlines():
                        if error_line.strip():
                            self.logger.error(error_line.strip())
                    self.logger.error('BRIQ_AssignSS stderr end')
                    raise RuntimeError('BRIQ_AssignSS failed\nstdout:\n%s\n\nstderr:\n%s\n' % (
                        stdout.decode('utf-8'), stderr[:500_000].decode('utf-8')))

                # generate BRIQ input file
                BRIQ_input = os.path.join(tmp_dir, 'input')

                with open(ss_tmp, 'r') as f:
                    lines = f.readlines()

                with open(BRIQ_input,'w') as f:
                    f.write(f'pdb {pdbin_tmp}\n')
                    f.writelines(lines)
                    if fix_non_paring_region:
                        wc = lines[1].strip().split()[1]
                        nwc = lines[2].strip().split()[1]
                        self.logger.info(f'sec {wc}')
                        self.logger.info(f'nwc {nwc}')
                        non_paring_indexs = [str(i) for i in range(len(wc)) if wc[i] == '.' and nwc[i] == '.']
                        non_paring_indexs = ' '.join(non_paring_indexs)
                        f.write(f'fixed {non_paring_indexs}\n')

            cmd = [f'{self.binary_dpath}/BRiQ_Refinement',
                    BRIQ_input,
                    pdbout_tmp,
                    str(self.random_seed)
            ]

            self.logger.info('Launching subprocess "%s"', ' '.join(cmd))
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            with timing(f'BRIQ Refinement', logger=self.logger):
                stdout, stderr = process.communicate()
                retcode = process.wait()

            if retcode:
                # Logs have a 15k character limit, so log BRIQ error line by line.
                self.logger.error('BRIQ  Refinement failed. BRIQ stderr begin:')
                for error_line in stderr.decode('utf-8').splitlines():
                    if error_line.strip():
                        self.logger.error(error_line.strip())
                self.logger.error('BRIQ Refinement stderr end')
                raise RuntimeError('BRIQ Refinement failed\nstdout:\n%s\n\nstderr:\n%s\n' % (
                    stdout.decode('utf-8'), stderr[:500_000].decode('utf-8')))

            self._rewrite_pdb_rm_H(pdbout_tmp, pdbout)
            self.logger.info('Export PDB file to %s' % pdbout)

    def _rewrite_pdb_rm_H(self, inp_fpath, out_fpath):
        tofile = open(out_fpath, 'w')

        with open(inp_fpath) as lines:
            for l in lines:
                if len(l.split()) > 5 and l.split()[0] == "ATOM":
                    atomn = l.split()[2]
                    if 'H' in atomn:
                        continue
                    tofile.write(l)

        tofile.close()