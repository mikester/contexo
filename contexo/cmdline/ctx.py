#!/usr/bin/env python

# coding=UTF-8
# TODO:  Files as arguments
import os
import os.path
import shutil
import string
from argparse import ArgumentParser
import argparse
from contexo import ctx_rspec
from contexo import ctx_view
from contexo import ctx_cfg
from contexo import ctx_cmod
from contexo.ctx_envswitch  import  assureList, EnvironmentLayout, switchEnvironment
from contexo import ctx_common
from contexo.ctx_common import getUserTempDir,  setInfoMessageVerboseLevel, infoMessage, userErrorExit, warningMessage, ctxAssert
from contexo.ctx_comp import ctx_log, COMPFile
from contexo import ctx_sysinfo

msgSender           = 'ctx.py'

#
# Get configuration.
#
cfgFile = ctx_cfg.CFGFile( os.path.join(ctx_common.getUserCfgDir(), 
                                        ctx_sysinfo.CTX_CONFIG_FILENAME))

#legacy code: to be rewritten
setInfoMessageVerboseLevel( int(cfgFile.getVerboseLevel()) )

CTX_DEFAULT_BCONF = cfgFile.getDefaultBConf().strip(" '")

#------------------------------------------------------------------------------
def getBuildConfiguration( cview ):
    from contexo import ctx_bc
    from contexo import config

    if args.bconf != None:
        bcFile = args.bconf
    else:
        if CTX_DEFAULT_BCONF != None:
            infoMessage( "Using default build configuration '%s'"%(CTX_DEFAULT_BCONF), 2, msgSender )
            bcFile = CTX_DEFAULT_BCONF
        else:
            userErrorExit( "No build configuration specified.", "ctx.py" )

    # Uglyness:
    # Historically the BCFile class located both the bc file and the cdef file 
    # on its own from a provided list of search locations. We work around this 
    # by providing only the single paths to these items which we get from the 
    # view (maintaining backward compatibility).
    # Since we don't know the name of the CDEF yet, we have to violate some 
    # good coding morale and extract it manually from the bc file. Some of this
    # code was copied from BCFile::__process_bc().
    
    # TODO: Make this a lot more pretty if possible..

    bcFilePath = cview.locateItem( bcFile, 'bconf' )
    bcFilename = os.path.basename( bcFilePath )
    bcPath = os.path.dirname( bcFilePath )

    bcDict = config.Config( bcFilePath )
    section = bcDict.get_section( 'config'  )
    if not section.has_key( 'CDEF' ):
        userErrorExit( "Mandatory BC option 'CDEF' is missing.", 'ctx.py on behalf of BCFile' )

    cdefFilename = section[ 'CDEF' ]
    cdefFilePath = cview.locateItem( cdefFilename, 'cdef' )
    cdefPath = os.path.dirname( cdefFilePath )
    
    ctxAssert( os.path.basename( cdefFilePath ).lower() == cdefFilename, "Something went wrong in our workaround.." )    
    
    bc = ctx_bc.BCFile( bcFilename, bcPath, cdefPath, cfgFile)

    return bc

#------------------------------------------------------------------------------
# TODO: Make recursive
def expand_list_files( view, item_list ):
    
    expanded_item_list = list()
    for item in item_list:
        item = item.strip(' ')
        if item.startswith('@'):
            infoMessage( "Expanding list file '%s'"%item, 2, msgSender )
            item = item.lstrip('@')
            list_file = view.locateItem( item, ctx_view.REPO_PATH_SECTIONS )
            list_file_items = ctx_common.readLstFile( list_file )
            expanded_item_list.extend( list_file_items )
        else:
            expanded_item_list.append(item)

    return expanded_item_list
            
    
#------------------------------------------------------------------------------
def getAccessPolicy( args ):
    
    if args.no_remote_repo_access == True:
        ap = ctx_view.AP_NO_REMOTE_ACCESS
    else:
        ap = ctx_view.AP_PREFER_REMOTE_ACCESS
        
    return ap
        
#------------------------------------------------------------------------------
# Creates and returns a list of CTXCodeModule objects from the provided list 
# of code module names. Unit tests are only enabled for main modules (not for 
# dependencies) 
#------------------------------------------------------------------------------
def create_codemodules( main_module_name_list, dep_module_name_list, 
                        unit_tests, module_search_paths  ):
    
    ctx_modules = list()

    all_modules = list()
    all_modules.extend(main_module_name_list)
    all_modules.extend(dep_module_name_list)      
    for module_name in all_modules:
        modRoot = ctx_cmod.resolveModuleLocation( module_name, 
                                                  module_search_paths )
        
        buildTest = unit_tests and module_name in main_module_name_list

        mod = ctx_cmod.CTXCodeModule( modRoot, 
                                      pathlist=None, 
                                      buildUnitTests = buildTest,
                                      forceRebuild=False )
        ctx_modules.append( mod )
    
    return ctx_modules

#------------------------------------------------------------------------------
# Creates and returns a list of CTXCodeModule objects from the provided list 
# of code module names. Unit tests are only enables for main modules (not for 
# dependencies) 
#------------------------------------------------------------------------------
def create_components( comp_filenames, component_paths ):

    # Construct and validate component objects
    components = list()
    for comp_file in comp_filenames:
        comp = COMPFile( comp_file, component_paths )
        components.append( comp )
        
    return components

#------------------------------------------------------------------------------
def build_libraries( ctx_modules, lib_name, output_path, build_dir, session ):

    #
    # Build either one library of all modules, or one library for each module.
    #

    if not os.path.exists( output_path ):
        os.makedirs( output_path )

    libs = dict()
    if lib_name != None:
        libs[lib_name] = assureList( ctx_modules )
    else:
        for mod in ctx_modules:
            libs[mod.getName()] = [mod,]
    
    for lib, mods in libs.iteritems():
        ctx_log.ctxlogBeginLibrary( lib )

        obj_list = list()
        for mod in mods:
            obj_list +=  mod.buildStaticObjects( session, None, build_dir )
        
        if len(obj_list) > 0:
            session.buildStaticLibrary( obj_list, lib, output_path )
        else:
            warningMessage( "No object files to create library '%s'"%(lib) )

        ctx_log.ctxlogEndLibrary()

        
#------------------------------------------------------------------------------
def export_public_module_headers ( depmgr, modules, headerPath ):

    if headerPath == None:
        return
        
    if not os.path.exists( headerPath ):
        os.makedirs( headerPath )

    publicHeaders = depmgr.getPublicHeaders(modules,  True)
    for publicHeader in publicHeaders:
        src = publicHeader
        dst = os.path.join( headerPath, os.path.basename(publicHeader) )
        infoMessage( "Exporting header: %s"%(os.path.basename(publicHeader)) )
        shutil.copyfile( src, dst )

#------------------------------------------------------------------------------
def humptydumpty_getFullPathname( header, codemodule_map ):
    
    if header in codemodule_map.keys():
        return codemodule_map[header]
    else:
        return None
    
#------------------------------------------------------------------------------
def export_headers( depmgr, headers, headerDir, codemodule_map ):

    if not os.path.exists( headerDir ):
        os.makedirs( headerDir )

    infoMessage( "Exporting headers", 1, msgSender )
    for header in headers:
        #src = depmgr.getFullPathname ( header )
        src = humptydumpty_getFullPathname ( header, codemodule_map ) # workaround
        if src != None:
            dst = os.path.join( headerDir, header )
            infoMessage( "%s -> %s"%(src, dst), 2, msgSender )
            shutil.copyfile( src, dst )
        else:
            warningMessage( "Unable to locate header '%s' for export"%(header), msgSender )

#------------------------------------------------------------------------------
def buildmodules( depmgr, session, modules, deps, tests, output_path, lib, 
                  build_dir ):
    from contexo import ctx_base
    from contexo import ctx_envswitch

    all_modules = depmgr.getCodeModules() if deps else modules
    all_modules.sort ()
    dep_modules = set(all_modules) - set(modules)

    ctx_modules = create_codemodules( modules, dep_modules, tests, 
                                      depmgr.getCodeModulePaths() )

    build_libraries( ctx_modules, lib, output_path, build_dir, session )

#------------------------------------------------------------------------------
def cmd_update(args):
    date_file = os.path.join ( ctx_common.getUserTempDir (), 'date_file.ctx' )
    if os.path.exists ( date_file ):
        os.remove ( date_file )
    import ctx_update_proxy

#------------------------------------------------------------------------------
def cmd_info(args):
    from contexo.ctx_depmgr import CTXDepMgr

    #
    # Get Code Module Paths from view.
    #

    cview = ctx_view.CTXView(args.view)
    CODEMODULE_PATHS = cview.getAllCodeModulePaths()

    #
    # Show info
    #

    print "Contexo version: ", ctx_sysinfo.CTX_DISPLAYVERSION
    print "Using build config file: ", CTX_DEFAULT_BCONF
    #print "Current view: ", CTX_DEFAULT_VIEW

    #
    # Module
    #

    if args.module != None:
        depmgr = CTXDepMgr ( args.module, CODEMODULE_PATHS, args.tests )
        module_names = depmgr.getCodeModules ()
        module_names.sort ()
        if len ( module_names ) > 0:
            print "\nDependency list:\n"
            for module in module_names:
                print "\t",module

        pub_headers = depmgr.getPublicHeaders ( args.module)
        pub_headers.sort()
        if len ( pub_headers ) > 0:
            print "\nDependent public headers:\n"
            for header in pub_headers:
                print "\t",header
#------------------------------------------------------------------------------
def cmd_buildmod(args):
    from contexo import ctx_cmod
    from contexo import ctx_base
    from contexo import ctx_envswitch
    from contexo.ctx_depmgr import CTXDepMgr

    # Switch to specified environment
    oldEnv = None
    if args.env != None:
        envLayout   = EnvironmentLayout( cfgFile,  args.env )
        oldEnv      = switchEnvironment( envLayout, True )

    if args.logfile != None:
        ctx_log.ctxlogStart()

    # Prepare all
    cview   = ctx_view.CTXView( args.view, getAccessPolicy(args), validate=bool(args.repo_validation) )
    modules = expand_list_files(cview, args.modules)
    bc      = getBuildConfiguration( cview )
    
    depmgr  = CTXDepMgr( modules, cview.getItemPaths('modules'), args.tests )
    session = ctx_base.CTXBuildSession( bc )
    session.setDependencyManager( depmgr )

    # Register build configuration in log handler
    ctx_log.ctxlogSetBuildConfig( bc.getTitle(),
                                  bc.getCompiler().cdefTitle,
                                  bc.getBuildParams().cflags,
                                  bc.getBuildParams().prepDefines,
                                  "N/A" )

    output_path = os.path.join( args.output, args.libdir )
    buildmodules( depmgr, session, modules, args.deps, args.tests,
                  output_path, args.lib, bc.getTitle() )

    header_path = os.path.join(args.output, args.headerdir )
    export_public_module_headers( depmgr, modules, header_path )

    # Write log if requested
    if args.logfile != None:
        logfilepath = os.path.join( args.output, args.logfile )
        logpath     = os.path.normpath(os.path.dirname( logfilepath ))
        if len(logpath) and not os.path.isdir(logpath):
            os.makedirs( logpath )
            
        ctx_log.ctxlogWriteToFile( logfilepath, appendToExisting=False )

    # Switch back to original environment
    if args.env != None:
        switchEnvironment( oldEnv, False )

#------------------------------------------------------------------------------
def cmd_buildcomp(args):
    from contexo import ctx_cmod
    from contexo import ctx_base
    from contexo import ctx_envswitch
    from contexo.ctx_depmgr import CTXDepMgr
    
    # Switch to specified environment
    oldEnv = None
    if args.env != None:
        envLayout = EnvironmentLayout( cfgFile,  args.env )
        oldEnv    = switchEnvironment( envLayout, True )

    if args.logfile != None:
        ctx_log.ctxlogStart()
    
    # Prepare all
    cview       = ctx_view.CTXView( args.view, getAccessPolicy(args), validate=bool(args.repo_validation) )
    components  = expand_list_files( cview, args.components )
    bc          = getBuildConfiguration( cview )
    #depmgr      = CTXDepMgr ( None, cview.getItemPaths('modules'), args.tests ) # See below comments about fix
    session     = ctx_base.CTXBuildSession( bc )
    #session.setDependencyManager( depmgr ) # See below comments about fix
    
    # Register build configuration in log handler
    ctx_log.ctxlogSetBuildConfig( bc.getTitle(),
                                  bc.getCompiler().cdefTitle,
                                  bc.getBuildParams().cflags,
                                  bc.getBuildParams().prepDefines,
                                  "N/A" )
        
    
    # Process components
    for comp in create_components( components, cview.getItemPaths('comp') ):
        ctx_log.ctxlogBeginComponent( comp.name )

        outputPath = os.path.join( args.output, comp.name )
        lib_dir = os.path.join( outputPath, args.libdir )
        header_dir = os.path.join( outputPath, args.headerdir )

        # Workaround to get header export to work
        codemodule_map = dict()
        
        # Build component modules.
        for library, modules in comp.libraries.items():
            
            modules = expand_list_files( cview, modules )            

            # TODO: Fix depmgr
            # This code is what should be enough. But instead
            # we have to create a new depmgr for each run since
            # something is left inconsistent when simply calling add/emptyCodeModules.
            
            #depmgr.addCodeModules( modules )
            #buildmodules( depmgr, session,  modules,  args.deps,  args.tests,  
            #              outputPath,  args.libdir,  library,  
            #              session.bc.getTitle())
            #depmgr.emptyCodeModules()

            # The below code is a workaround to make unittests work for components


            depmgr      = CTXDepMgr ( modules, cview.getItemPaths('modules'), args.tests )
            session.setDependencyManager( depmgr )
            buildmodules( depmgr, session,  modules,  args.deps,  args.tests,
                          lib_dir, library, session.bc.getTitle())
                          
            # Oh horrific
            codemodule_map.update( depmgr.inputFilePathDict )
            

        # The above fix has the side-effect that it's only the last constructed
        # depmgr which is used for this header-export. To get around this we
        # temporarily add an extra argument to this function (last one)
        export_headers( depmgr, comp.publicHeaders, header_dir, codemodule_map )
    
        ctx_log.ctxlogEndComponent()

    # Write log if requested
    if args.logfile != None:
        logfilepath = os.path.join( args.output, args.logfile )
        logpath     = os.path.normpath(os.path.dirname( logfilepath ))
        if len(logpath) and not os.path.isdir(logpath):
            os.makedirs( logpath )
            
        ctx_log.ctxlogWriteToFile( logfilepath, appendToExisting=False )


    # Restore environment   
    if args.env != None:
        switchEnvironment( oldEnv, False )

#------------------------------------------------------------------------------
def cmd_export(args):
    from contexo import ctx_cmod
    from contexo import ctx_base
    from contexo import ctx_envswitch
    from contexo.ctx_depmgr import CTXDepMgr
    from contexo.ctx_export import CTXExportData
    
    envLayout = None
    oldEnv = None
    if args.env != None:
        envLayout = EnvironmentLayout( cfgFile,  args.env )
        oldEnv    = switchEnvironment( envLayout, True )

    # Prepare all
    cview   = ctx_view.CTXView( args.view, getAccessPolicy(args), validate=bool(args.repo_validation) )
    bc      = getBuildConfiguration( cview )
    depmgr  = CTXDepMgr ( None, cview.getItemPaths('modules'), args.tests )
    session = ctx_base.CTXBuildSession( bc )
    session.setDependencyManager( depmgr )

    export_items = expand_list_files( cview, args.export_items )
    
    # Make sure we have only one type of item to export
    component_export = True
    for item in export_items:
        if item.endswith( '.comp' ):
            if component_export == False:
                userErrorExit( "An export operation can either export a list of components OR a list of modules, not both.", msgSender)
        else:
            component_export = False
            
    components   = list()
    main_modules = list() # Excluding dependency modules
    if component_export:
        # Construct and validate component objects
        components = create_components( export_items, cview.getItemPaths('comp') )
        for comp in components:
            for library, compmodules in comp.libraries.items():
                depmgr.addCodeModules( compmodules )
                main_modules.extend( compmodules )
    else:
        main_modules = export_items
           
    # Divert modules into main modules and dependency modules
    export_modules = depmgr.getCodeModules() if args.deps else main_modules
    export_modules.sort()
    dep_modules = set(export_modules) - set(main_modules)
    ctx_modules = create_codemodules( main_modules, dep_modules, args.tests, cview.getItemPaths('modules') )
    
    module_map = dict()
    for mod in ctx_modules:
        module_map[mod.getName()] = mod
    
    depmgr.updateDependencyHash()

    # Dispatch export data to handler (through pipe)
    package = CTXExportData()
    package.setExportData( module_map, components, None, session, depmgr, 
                           cview, envLayout, args )
    package.dispatch()
    
    # Restore environment
    if args.env != None:
        switchEnvironment( oldEnv, False )

#------------------------------------------------------------------------------
def cmd_updateview(args):

    if args.updates_only == True and args.checkouts_only == True:
        userErrorExit( "Options '--updates_only' and '--checkouts-only' are mutually exclusive.", msgSender )

    cview = ctx_view.CTXView( args.view, getAccessPolicy(args), updating=True, validate=True )

    if args.checkouts_only == False:
        cview.updateRepositories()
        
    if args.updates_only == False:
        cview.checkoutRepositories()
    
#------------------------------------------------------------------------------
def cmd_validateview(args):

    # The view will validate itself in the constructor
    cview = ctx_view.CTXView( args.view, getAccessPolicy(args), validate=True )

    infoMessage( "Validation complete", 1, msgSender )

#------------------------------------------------------------------------------
def cmd_clean(args):

    from contexo import ctx_cmod
    from contexo.ctx_depmgr import CTXDepMgr

    #
    # Get Code Module Paths from view.
    #

    cview = getViewDefinition(args.view)
    CODEMODULE_PATHS = cview.getAllCodeModulePaths()

    #
    # Get build configuration.
    #

    bc = getBuildConfiguration (args.bconf)

    #
    # Determine all module dependencies.
    #

    exp_modules = expand_list_files( cview, args.modules )

    depmgr = CTXDepMgr ( exp_modules, CODEMODULE_PATHS, args.tests )
    if args.d:
        module_names = depmgr.getCodeModules ()
    else:
        module_names = exp_modules

    #
    # Create contexo modules
    # TODO: Get the CTXCodeModules to avoid this extra CodeModule creation.

    modules = list ()
    modRoots = list ()
    for module in exp_modules:
        modRoot = ctx_cmod.resolveModuleLocation (module, CODEMODULE_PATHS)
        mod = ctx_cmod.CTXCodeModule(modRoot, pathlist=None, buildUnitTests = args.tests, forceRebuild=False)
        modules.append( mod )
        modRoots.append ( modRoot )

    for module in set(module_names) - set(exp_modules):
        modRoot = ctx_cmod.resolveModuleLocation (module, CODEMODULE_PATHS)
        mod = ctx_cmod.CTXCodeModule(modRoot, pathlist=None, buildUnitTests = args.tests, forceRebuild=False)
        modules.append( mod )
        modRoots.append ( modRoot )

    depmgr.addCodeModules ( modRoots, args.tests )

    print "cleaning modules:"

    for module in modules:
        print module.getName()
        module.clean (bc.getTitle())

#------------------------------------------------------------------------------
def cmd_importview(args):

    for file in args.files:
        (dummy,  viewdefname)= os.path.split(file)
        (viewname,  ext) = os.path.splitext(viewdefname)
        dirname = os.path.join('.', args.destination.pop(),  viewname)
        if os.path.isdir(dirname ):
            confirmation = ''
            while confirmation != 'yes' and confirmation != 'no':
                confirmation = raw_input('Directory ' + dirname + ' already exists. It will be removed. Continue? [yes/no]: ')
            if (confirmation == 'yes'):
                shutil.rmtree(dirname)
            else:
                return
        os.makedirs (dirname,  0755)
        shutil.copy2(file,  dirname)

#------------------------------------------------------------------------------
def cmd_view(args):

    #if args.fromfile.__len__() > 0:


    cview = getViewDefinition(args.view)

    if args.switch:
        default_view = cfgFile.getDefaultView()

        new_default_view = os.path.abspath (args.view)

        cfgFile.setDefaultView ( new_default_view )

        print 'switch view: ', default_view + " -> " + new_default_view

        cfgFile.update()



    if args.i:
        # Parse rspec file
        cview.printView ()

    if args.checkout:
        cview.checkout ()
        cview.printView ()

    if args.create:
        cview = ctx_view.CTXView ()
        cview.printView()

#------------------------------------------------------------------------------
def cmd_prop(args):

    available_properties = ['bconf','rspec','verb','bconf_paths', \
                            'cdef_paths', 'env_paths']

    if args.property is None:
        args.property = available_properties
        args.set = None
    else:
        if args.property not in available_properties:
            print "ctx.py: error: property not available"

    if 'bconf' in args.property:
        print '"bconf" Current build configuration: ', cfgFile.getDefaultBConf()
        if args.set is not None:
            cfgFile.setDefaultBConf ( os.path.normpath (args.set) )
            print "Changed to: ", args.set

    if 'rspec' in args.property:
        print '"rspec" Current view : ', cfgFile.getDefaultView()
        if args.set is not None:
            cfgFile.setDefaultView ( os.path.normpath (args.set) )
            print "Changed to: ", args.set

    if 'bconf_paths' in args.property:
        print '"bconf_paths" Build configuration paths: ', cfgFile.getBConfPaths()
        if args.set is not None:
            cfgFile.setBConfPaths ( os.path.normpath (args.set) )
            print "Changed to: ", args.set

        if args.add is not None:
            pass

    if 'cdef_paths' in args.property:
        print '"cdef_paths" Compiler definition paths: ', cfgFile.getCDefPaths()

    if 'env_paths' in args.property:
        print '"env_paths" Enviroment file paths: ', cfgFile.getEnvPaths()

    if 'verb' in args.property:
        print '"verb" Verbosity level: ', cfgFile.getVerboseLevel()

    cfgFile.update()


###############################################################################
# ENTRY POINT


# Create Parser
parser = ArgumentParser( description=ctx_sysinfo.CTX_BANNER, 
                         version=ctx_sysinfo.CTX_DISPLAYVERSION,
                         prog="ctx" )

subparsers = parser.add_subparsers ()

# update parser
#parser_update = subparsers.add_parser('update', help="check if updates are available and update accordingly" )
#parser_update.set_defaults(func=update)

# info parser
# TODO: Implement 'info' command
#parser_info = subparsers.add_parser('info', help="Displays useful information about the build system")
#parser_info.set_defaults(func=info)
#parser_info.add_argument('-m', '--module', nargs=1, help="Show info about a contexo module")
#parser_info.add_argument('-t', action='store_true', help="Show info on both module and unit tests")
#parser_info.add_argument('-v', '--view', help="The view to use for this operation")

standard_description = dict({\
    '--bconf': "Build configuration file (*.bc/*.bconf)",\
      '--env': "One or more enviroment replacement files (*.env)",\
   '--output': "The location (path) in which to place output files",\
   '--libdir': "Relative directory within '--output' in which to place built library files. Will be created if not already present.",\
'--headerdir': "Directory name within '--output' in which to place exported header files. Will be created if not already present.",\
     '--deps': "If specified, all dependencies (modules) are processed as well.",\
    '--tests': "If specified, the unit tests for each processed code module are included as well.",\
     '--view': "The local view directory to use for this operation. If omitted, current working directory is used.",\
  '--logfile': "Name of logfile to generate. Will be created in output folder as defined by the --output option.",\
'--repo-validation': "Validates all repositories before processing. This usually increases duration but ensures correct repository structure. Repository validation can also be done by running 'ctx view validate' as a separate step.",\
'--no-remote-repo-access': "If specified, the system never tries to process items directly from an RSpec repository's remote location (href) even if so is possible. Normally, if a repository is accessible through regular file access, the system always tries to use it from its remote location."})


# buildmod parser
parser_build = subparsers.add_parser('buildmod', help="build contexo modules." )
parser_build.set_defaults(func=cmd_buildmod)
parser_build.add_argument('modules', nargs='+', help="list of modules to build" )
parser_build.add_argument('-b', '--bconf', help=standard_description['--bconf'] )
parser_build.add_argument('-e', '--env', help=standard_description['--env'] )
parser_build.add_argument('-o', '--output', default=os.getcwd(), help=standard_description['--output'])
parser_build.add_argument('-ld','--libdir', default="", help=standard_description['--libdir'])
parser_build.add_argument('-hd','--headerdir', default="", help=standard_description['--headerdir'])
parser_build.add_argument('-l', '--lib', help="if the build operation results in a single library, this option sets its name")
parser_build.add_argument('-d', '--deps', action='store_true', help=standard_description['--deps'])
parser_build.add_argument('-t', '--tests', action='store_true', help=standard_description['--tests'])
parser_build.add_argument('-v', '--view', default=os.getcwd(), help=standard_description['--view'])
parser_build.add_argument('-lf', '--logfile', default=None, help=standard_description['--logfile'])
parser_build.add_argument('-rv', '--repo-validation', action='store_true', help=standard_description['--repo-validation'])
parser_build.add_argument('-nra', '--no-remote-repo-access', action='store_true', help=standard_description['--no-remote-repo-access'])

# buildcomp parser
parser_build = subparsers.add_parser('buildcomp', help="build contexo components.")
parser_build.set_defaults(func=cmd_buildcomp)
parser_build.add_argument('components', nargs='+', help="list of components to build")
parser_build.add_argument('-b', '--bconf', help=standard_description['--bconf'])
parser_build.add_argument('-e', '--env', help=standard_description['--env'])
parser_build.add_argument('-o', '--output', default=os.getcwd(), help=standard_description['--output'])
parser_build.add_argument('-ld','--libdir', default="", help=standard_description['--libdir'])
parser_build.add_argument('-hd','--headerdir', default="", help=standard_description['--headerdir'])
parser_build.add_argument('-d', '--deps', action='store_true', help=standard_description['--deps'])
parser_build.add_argument('-t', '--tests', action='store_true', help=standard_description['--tests'])
parser_build.add_argument('-v', '--view', default=os.getcwd(), help=standard_description['--view'])
parser_build.add_argument('-lf', '--logfile', default=None, help=standard_description['--logfile'])
parser_build.add_argument('-rv', '--repo-validation', action='store_true', help=standard_description['--repo-validation'])
parser_build.add_argument('-nra', '--no-remote-repo-access', action='store_true', help=standard_description['--no-remote-repo-access'])

# clean parser
parser_clean = subparsers.add_parser('clean', help="clean a module(s) ( and optionaly its dependencies)")
parser_clean.set_defaults(func=cmd_clean)
parser_clean.add_argument('modules', nargs='+', help="list of modules to clean")
parser_clean.add_argument('-d', '--deps', action='store_true', help=standard_description['--deps'])
parser_clean.add_argument('-b', '--bconf', help="only clean target files produced from this build configuration.")
parser_clean.add_argument('-t', '--tests', action='store_true', help=standard_description['--tests'])
parser_clean.add_argument('-v', '--view', help=standard_description['--view'])
parser_clean.add_argument('-rv', '--repo-validation', action='store_true', help=standard_description['--repo-validation'])
parser_clean.add_argument('-nra', '--no-remote-repo-access', action='store_true', help=standard_description['--no-remote-repo-access'])



#
# export parser
#


export_usage = """

-- USAGE NOTES ------------------------------------------------------

The export command is a plugin interface to Contexo which utilizes
the 'pipe' mechanism to communicate build session data.
Example, exporting to the 'msvc' plugin:

ctx export my.comp -bc my.bc | msvc -pn my_vcproj_title -o out_folder

Instead of building, Contexo transfers the build session data to the 
MSVC plugin which in turn renders a Visual Studio project from the
information.

To invoke commandline help for a certain plugin, use the help option 
for both ctx and the plugin:

ctx export --help | msvc --help

---------------------------------------------------------------------
"""

parser_export = subparsers.add_parser('export', help="Export utilities.")
parser_export.set_defaults(func=cmd_export)
parser_export.formatter_class = argparse.RawDescriptionHelpFormatter
parser_export.description = export_usage
parser_export.add_argument('export_items', nargs='*', default="", help="List of items to export. Can be omitted if the export plugin doesn't require any items. Code modules and components cannot be mixed in the same export operation.")
parser_export.add_argument('-b', '--bconf', help=standard_description['--bconf'])
parser_export.add_argument('-e', '--env', help=standard_description['--env'])
parser_export.add_argument('-v', '--view', default=os.getcwd(), help=standard_description['--view'])
parser_export.add_argument('-d', '--deps', action='store_true', help=standard_description['--deps'])
parser_export.add_argument('-t', '--tests', action='store_true', help=standard_description['--tests'])
parser_export.add_argument('-o', '--output', default=os.getcwd(), help=standard_description['--output'])
parser_export.add_argument('-rv', '--repo-validation', action='store_true', help=standard_description['--repo-validation'])
parser_export.add_argument('-nra', '--no-remote-repo-access', action='store_true', help=standard_description['--no-remote-repo-access'])

#
#
#

parser_view = subparsers.add_parser( 'view', help="View operations" )
view_subparsers = parser_view.add_subparsers()

parser_view_update = view_subparsers.add_parser('update', help="Update/synchronize a view")
parser_view_update.set_defaults(func=cmd_updateview)
parser_view_update.add_argument('view', nargs='?', default=os.getcwd(), help="Relative or absolute path to a view directory. If omitted, current working directory is used.")
parser_view_update.add_argument('-co', '--checkouts-only', action='store_true', help="Checkout missing repositories only. Don't update existing repositories.")
parser_view_update.add_argument('-uo', '--updates-only', action='store_true', help="Update existing repositories only. Don't checkout missing repositories.")
parser_view_update.add_argument('-nra', '--no-remote-repo-access', action='store_true', help="Always checkout/update repositories into the local view, even if they are accessible from their remote location. If this flag is used with other commands, it may conveniently be used here as well to avoid having to update such repositories manually.")

parser_view_validate = view_subparsers.add_parser('validate', help="Validate consistency of view structure")
parser_view_validate.set_defaults(func=cmd_validateview)
parser_view_validate.add_argument('view', nargs='?', default=os.getcwd(), help="Relative or absolute path to a view directory. If omitted, current working directory is used.")
parser_view_validate.add_argument('-nra', '--no-remote-repo-access', action='store_true', help="Repositories which can be remotely accessed are still invalidated if not present in view.")

###############################################################################

# Parse cmdline
args = parser.parse_args()
args.func(args)
