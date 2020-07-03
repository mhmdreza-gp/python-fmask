"""
Script that takes a stacked Sentinel 2 Level 1C image and runs
fmask on it.
"""
# This file is part of 'python-fmask' - a cloud masking module
# Copyright (C) 2015  Neil Flood
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 3
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
from __future__ import print_function, division

import sys
import os
import argparse
import numpy
import tempfile
import glob
import subprocess

from rios import fileinfo
from rios.imagewriter import DEFAULTDRIVERNAME, dfltDriverOptions
from rios.parallel.jobmanager import find_executable

from fmask import config
from fmask import fmaskerrors
from fmask.cmdline import sentinel2makeAnglesImage
from fmask import fmask

# for GDAL command line utilities
CMDLINECREATIONOPTIONS = []
if DEFAULTDRIVERNAME in dfltDriverOptions:
    for opt in dfltDriverOptions[DEFAULTDRIVERNAME]:
        CMDLINECREATIONOPTIONS.append('-co')
        CMDLINECREATIONOPTIONS.append(opt)

if sys.platform.startswith('win'):
    GDALWARPCMDNAME = "gdalwarp.exe"
else:
    GDALWARPCMDNAME = "gdalwarp"
GDALWARPCMD = find_executable(GDALWARPCMDNAME)
if GDALWARPCMD is None:
    msg = "Unable to find {} command. Check installation of GDAL package".format(GDALWARPCMDNAME)
    raise fmaskerrors.FmaskInstallationError(msg)


def getCmdargs(argv=None):
    """
    Get command line arguments
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--safedir", help=("Name of .SAFE directory, as unzipped from " +
        "a standard ESA L1C zip file. Using this option will automatically create intermediate " +
        "stacks of the input bands, and so does NOT require --toa or --anglesfile. "))
    parser.add_argument("--granuledir", help=("Name of granule sub-directory within the " +
        ".SAFE directory, as unzipped from a standard ESA L1C zip file. This option is an " +
        "alternative to --safedir, for use with ESA's old format zipfiles which had multiple " +
        "granules in each zipfile. Specify the subdirectory of the single tile, under the " +
        "<safedir>/GRANULE/ directory. " +
        "Using this option will automatically create intermediate " +
        "stacks of the input bands, and so does NOT require --toa or --anglesfile. "))
    parser.add_argument('-a', '--toa', 
        help=('Input stack of TOA reflectance (as supplied by ESA). This is obsolete, and is ' +
            'only required if NOT using the --safedir or --granuledir option. '))
    parser.add_argument('-z', '--anglesfile', 
        help=("Input angles file containing satellite and sun azimuth and zenith. " +
            "See fmask_sentinel2makeAnglesImage.py for assistance in creating this. " +
            "This option is obsolete, and is only required if NOT using the --safedir " +
            "or --granuledir option. "))
    parser.add_argument('-o', '--output', help='Output cloud mask')
    parser.add_argument('-v', '--verbose', dest='verbose', default=False,
        action='store_true', help='verbose output')
    parser.add_argument("--pixsize", default=20, type=int, 
        help="Output pixel size in metres (default=%(default)s)")
    parser.add_argument('-k', '--keepintermediates', 
        default=False, action='store_true', help='Keep intermediate temporary files (normally deleted)')
    parser.add_argument('-e', '--tempdir', 
        default='.', help="Temp directory to use (default='%(default)s')")
    
    params = parser.add_argument_group(title="Configurable parameters", description="""
        Changing these parameters will affect the way the algorithm works, and thus the 
        quality of the final output masks. 
        """)
    params.add_argument("--mincloudsize", type=int, default=0, 
        help="Mininum cloud size (in pixels) to retain, before any buffering. Default=%(default)s)")
    params.add_argument("--cloudbufferdistance", type=float, default=150,
        help="Distance (in metres) to buffer final cloud objects (default=%(default)s)")
    params.add_argument("--shadowbufferdistance", type=float, default=300,
        help="Distance (in metres) to buffer final cloud shadow objects (default=%(default)s)")
    defaultCloudProbThresh = 100 * config.FmaskConfig.Eqn17CloudProbThresh
    params.add_argument("--cloudprobthreshold", type=float, default=defaultCloudProbThresh,
        help=("Cloud probability threshold (percentage) (default=%(default)s). This is "+
            "the constant term at the end of equation 17, given in the paper as 0.2 (i.e. 20%%). "+
            "To reduce commission errors, increase this value, but this will also increase "+
            "omission errors. "))
    dfltNirSnowThresh = config.FmaskConfig.Eqn20NirSnowThresh
    params.add_argument("--nirsnowthreshold", default=dfltNirSnowThresh, type=float,
        help=("Threshold for NIR reflectance (range [0-1]) for snow detection "+
            "(default=%(default)s). Increase this to reduce snow commission errors"))
    dfltGreenSnowThresh = config.FmaskConfig.Eqn20GreenSnowThresh
    params.add_argument("--greensnowthreshold", default=dfltGreenSnowThresh, type=float,
        help=("Threshold for Green reflectance (range [0-1]) for snow detection "+
            "(default=%(default)s). Increase this to reduce snow commission errors"))
    params.add_argument("--parallaxtest", default=False, action="store_true",
        help="Turn on the parallax displacement test from Frantz (2018) (default will not use this test)")

    cmdargs = parser.parse_args(argv)

    # Do some sanity checks on what was given
    safeDirGiven = (cmdargs.safedir is not None)
    granuleDirGiven = (cmdargs.granuledir is not None)
    if granuleDirGiven and safeDirGiven:
        print("Only give one of --safedir or --granuledir. The --granuledir is only ")
        print("required for multi-tile zipfiles in the old ESA format")
        sys.exit(1)
    stackAnglesGiven = (cmdargs.toa is not None and cmdargs.anglesfile is not None)
    multipleInputGiven = (safeDirGiven or granuleDirGiven) and stackAnglesGiven
    inputGiven = safeDirGiven or granuleDirGiven or stackAnglesGiven
    if cmdargs.output is None or multipleInputGiven or not inputGiven:
        parser.print_help()
        sys.exit(1)
    
    return cmdargs


def checkAnglesFile(inputAnglesFile, toafile):
    """
    Check that the resolution of the input angles file matches that of the input
    TOA reflectance file. If not, make a VRT file which will resample it 
    on-the-fly. Only checks the resolution, assumes that if these match, then everything
    else will match too. 
    
    Return the name of the angles file to use. 
    
    """
    toaImgInfo = fileinfo.ImageInfo(toafile)
    anglesImgInfo = fileinfo.ImageInfo(inputAnglesFile)

    
    outputAnglesFile = inputAnglesFile
    if (toaImgInfo.xRes != anglesImgInfo.xRes) or (toaImgInfo.yRes != anglesImgInfo.yRes):
        (fd, vrtName) = tempfile.mkstemp(prefix='angles', suffix='.vrt')
        os.close(fd)
        subprocess.check_call([GDALWARPCMD, '-q', '-of', 'VRT', '-tr', 
            str(toaImgInfo.xRes), str(toaImgInfo.yRes), '-te', str(toaImgInfo.xMin),
            str(toaImgInfo.yMin), str(toaImgInfo.xMax), str(toaImgInfo.yMax),
            '-r', 'near', inputAnglesFile, vrtName])
        outputAnglesFile = vrtName
    
    return outputAnglesFile


def makeStackAndAngles(cmdargs):
    """
    Make an intermediate stack of all the TOA reflectance bands. Also make an image
    of the angles. Fill in the names of these in the cmdargs object. 
        
    """
    if cmdargs.granuledir is None and cmdargs.safedir is not None:
        cmdargs.granuledir = findGranuleDir(cmdargs.safedir)

    # Find the other commands we need, even under Windoze
    gdalmergeCmd = find_executable("gdal_merge.py")
    if gdalmergeCmd is None:
        msg = "Unable to find gdal_merge.py command. Check installation of GDAL package. "
        raise fmaskerrors.FmaskInstallationError(msg)

    # Make the angles file
    (fd, anglesfile) = tempfile.mkstemp(dir=cmdargs.tempdir, prefix="angles_tmp_", 
        suffix=".img")
    os.close(fd)
    xmlfile = findGranuleXml(cmdargs.granuledir)
    if cmdargs.verbose:
        print("Making angles image")
    sentinel2makeAnglesImage.makeAngles(xmlfile, anglesfile)
    cmdargs.anglesfile = anglesfile
    
    # Make a stack of the reflectance bands. Not that we do an explicit resample to the
    # output pixel size, to avoid picking up the overview layers with the ESA jpg files. 
    # According to @vincentschut, these are shifted slightly, and should be avoided.
    bandList = ['B01', 'B02', 'B03', 'B04', 'B05', 'B06', 'B07', 'B08', 'B8A',
        'B09', 'B10', 'B11', 'B12']
    imgDir = "{}/IMG_DATA".format(cmdargs.granuledir)
    resampledBands = []
    for band in bandList:
        (fd, tmpBand) = tempfile.mkstemp(dir=cmdargs.tempdir, prefix="tmp_{}_".format(band),
            suffix=".vrt")
        os.close(fd)
        inBandImgList = glob.glob("{}/*_{}.jp2".format(imgDir, band))
        if len(inBandImgList) != 1:
            raise fmaskerrors.FmaskFileError("Cannot find input band {}".format(band))
        inBandImg = inBandImgList[0]

        # Now make a resampled copy to the desired pixel size, using the right resample method
        resampleMethod = chooseResampleMethod(cmdargs.pixsize, inBandImg)
        subprocess.check_call([GDALWARPCMD, '-q', '-tr', str(cmdargs.pixsize),
                str(cmdargs.pixsize), '-co', 'TILED=YES', '-of', 'VRT', '-r',
                resampleMethod, inBandImg, tmpBand])
        
        resampledBands.append(tmpBand)
    
    # Now make a stack of these
    if cmdargs.verbose:
        print("Making stack of all bands, at {}m pixel size".format(cmdargs.pixsize))
    (fd, tmpStack) = tempfile.mkstemp(dir=cmdargs.tempdir, prefix="tmp_allbands_",
        suffix=".img")
    os.close(fd)
    cmdargs.toa = tmpStack
    # ensure we are launching python rather than relying on the OS to do the right thing (ie Windows)
    subprocess.check_call([sys.executable, gdalmergeCmd, '-q', '-of', 
            DEFAULTDRIVERNAME] + CMDLINECREATIONOPTIONS + ['-separate', 
        '-o', cmdargs.toa] + resampledBands)
    
    for fn in resampledBands:
        os.remove(fn)
    
    return resampledBands


def chooseResampleMethod(outpixsize, inBandImg):
    """
    Choose the right resample method, given the image and the desired output pixel size
    """
    imginfo = fileinfo.ImageInfo(inBandImg)
    inPixsize = imginfo.xRes
    
    if outpixsize == inPixsize:
        resample = "near"
    elif outpixsize > inPixsize:
        resample = "average"
    else:
        resample = "cubic"
    
    return resample


def findGranuleDir(safedir):
    """
    Search the given .SAFE directory, and find the main XML file at the GRANULE level.
    
    Note that this currently only works for the new-format zip files, with one 
    tile per zipfile. The old ones are being removed from service, so we won't 
    cope with them. 
    
    """
    granuleDirPattern = "{}/GRANULE/L1C_*".format(safedir)
    granuleDirList = glob.glob(granuleDirPattern)
    if len(granuleDirList) == 0:
        raise fmaskerrors.FmaskFileError("Unable to find GRANULE sub-directory {}".format(granuleDirPattern))
    elif len(granuleDirList) > 1:
        dirstring = ','.join(granuleDirList)
        msg = "Found multiple GRANULE sub-directories: {}".format(dirstring)
        raise fmaskerrors.FmaskFileError(msg)
    
    granuleDir = granuleDirList[0]
    return granuleDir


def findGranuleXml(granuleDir):
    """
    Find the granule-level XML file, given the granule dir
    """
    xmlfile = "{}/MTD_TL.xml".format(granuleDir)
    if not os.path.exists(xmlfile):
        # Might be old-format zipfile, so search for *.xml
        xmlfilePattern = "{}/*.xml".format(granuleDir)
        xmlfileList = glob.glob(xmlfilePattern)
        if len(xmlfileList) == 1:
            xmlfile = xmlfileList[0]
        else:
            raise fmaskerrors.FmaskFileError("Unable to find XML file {}".format(xmlfile))
    return xmlfile


def mainRoutine(argv=None):
    """
    Main routine that calls fmask
    """
    cmdargs = getCmdargs(argv)
    tempStack = False
    if cmdargs.safedir is not None or cmdargs.granuledir is not None:
        tempStack = True
        resampledBands = makeStackAndAngles(cmdargs)
    
    anglesfile = checkAnglesFile(cmdargs.anglesfile, cmdargs.toa)
    anglesInfo = config.AnglesFileInfo(anglesfile, 3, anglesfile, 2, anglesfile, 1, anglesfile, 0)
    
    fmaskFilenames = config.FmaskFilenames()
    fmaskFilenames.setTOAReflectanceFile(cmdargs.toa)
    fmaskFilenames.setOutputCloudMaskFile(cmdargs.output)
    
    fmaskConfig = config.FmaskConfig(config.FMASK_SENTINEL2)
    fmaskConfig.setAnglesInfo(anglesInfo)
    fmaskConfig.setKeepIntermediates(cmdargs.keepintermediates)
    fmaskConfig.setVerbose(cmdargs.verbose)
    fmaskConfig.setTempDir(cmdargs.tempdir)
    fmaskConfig.setTOARefScaling(10000.0)
    fmaskConfig.setMinCloudSize(cmdargs.mincloudsize)
    fmaskConfig.setEqn17CloudProbThresh(cmdargs.cloudprobthreshold / 100)    # Note conversion from percentage
    fmaskConfig.setEqn20NirSnowThresh(cmdargs.nirsnowthreshold)
    fmaskConfig.setEqn20GreenSnowThresh(cmdargs.greensnowthreshold)
    fmaskConfig.setSen2displacementTest(cmdargs.parallaxtest)
    
    # Work out a suitable buffer size, in pixels, dependent on the resolution of the input TOA image
    toaImgInfo = fileinfo.ImageInfo(cmdargs.toa)
    fmaskConfig.setCloudBufferSize(int(cmdargs.cloudbufferdistance / toaImgInfo.xRes))
    fmaskConfig.setShadowBufferSize(int(cmdargs.shadowbufferdistance / toaImgInfo.xRes))
    
    fmask.doFmask(fmaskFilenames, fmaskConfig)
    
    if (anglesfile != cmdargs.anglesfile):
        # Must have been a temporary, so remove it
        os.remove(anglesfile)
    
    if tempStack and not cmdargs.keepintermediates:
        for fn in [cmdargs.toa, cmdargs.anglesfile]:
            if os.path.exists(fn):
                os.remove(fn)
    
