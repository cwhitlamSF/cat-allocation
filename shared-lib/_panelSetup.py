#Dependencies
from modelkit.specs import configparser_to_dict, convertDictToTable, panelSpecKeys
from modelkit.frames import dfReplaceNanNone
from modelkit.files import createFolderIfNot
from modelkit.paths import panel_resource_path
import RSA_Functions as mFns
import RSA_PanelFunctions as pFns
import param
import _dataformClass as dfc
import _Icons as icons
import xlwings as xw
import os
import _chartClasses as cc
import _analysis as analysis
import polars as pl
import panel as pn
from bokeh.plotting import figure
from bokeh.models import ColumnDataSource, Legend, LegendItem, NumeralTickFormatter
from bokeh.models.widgets.tables import NumberFormatter, BooleanFormatter,StringFormatter,DateFormatter,CheckboxEditor, NumberEditor, SelectEditor, StringEditor, DateEditor
import base64
import panel as pn
import shutil
from IPython.display import display
import logging
import logging
import zipfile
from panel_modal import Modal

MYLOGGER = logging.getLogger(__name__)

DEBUGGER =pn.widgets.Debugger(name='Debugger debug level', level=logging.DEBUG, sizing_mode='stretch_both',logger_names=['PanelSetup','Analysis','Functions','Misc','Dataforms','Charts'],collapsed=False)
RAW_CSS="""
.navbar-toggler-icon {
  background-image: url("data:image/svg+xml;charset=utf8,%3Csvg viewBox='0 0 32 32' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath stroke='rgba(29,90,165, 1)' stroke-width='2' stroke-linecap='round' stroke-miterlimit='10' d='M4 8h24M4 16h24M4 24h24'/%3E%3C/svg%3E");
}
.title {color:#1d5aa5 !important; }
.sidenav#sidebar {
    background-color: #ffffff;
}
"""


class Panel(param.Parameterized):
    analysisname=param.String()
    
    def __init__(self, connectiontype,modeltype,modelprefix,userid='None',configfile=None,**params):
        super().__init__(**params)
        MYLOGGER.debug('Starting Panel class initialization')
        
        self.connectiontype=connectiontype
        self.userid=userid
        self.modeltype=modeltype  
        self.modelprefix=modelprefix
        self.filename=''
        self.filelist=[]
        self.analysislist=[]
        self.analysis=None
        self.updating=False  
        self.eventwatches=[]
        self.templist=[]
        self.panelDicts={}
        self.widgetDict={}
        self.widgetGroupDict={}
        self.widgetGroupLevelsDict={}
        self.dataformWidgetBlanksDict: dict = {}
        self.specWidgetDict: dict = {}
        self.dataformWidgetDict: dict = {}
        self.dataformDict: dict = {}
        self.tabsDict={}
        self.mainMenuButtonDict={}  #dict to store main menu buttons 
        self.mainmenuwidgets=[] 
        # Full path to <MODELPREFIX>_config.ini (main.py passes it); relative name as a fallback.
        self.configfile=configfile or self.modelprefix+'_config.ini'
        self.configdict=configparser_to_dict(self.configfile)
        # Spec Keys flagged Panel = True in the config's [Panel Flags] section
        self.panelDictList=panelSpecKeys(self.configdict)
        
        self.mainAreaWidget=[pn.Column(pn.pane.Markdown("Welcome to the "+str(self.modeltype),width=200))]
       
       #css
        self.css = '''
                .test button{
                    white-space: normal !important;
                    word-break:break-all;
                    width:150px
                    }

                .test .bk-btn.bk-btn-light{
                    text-align: left;
                    }
                    
                .test .bk-btn-group{
                    display: inline;
                    align-items: left;
                }
                '''
        #switched from #f5f5f5 to #ffffff
        self.mainmenucss = '''
                .bk-btn-group{
                    display: inline-block;
                    align-items: left;}

                .bk-btn.bk-btn-light{
                    background-color: #ffffff; 
                }

                .bk-btn.bk-btn-light:hover{
                    background-color: #ffffff;
                    font-weight: bold;
                    color: #1d5aa5;
                }

                .bk-btn.bk-btn-light:focus{
                    background-color: #ffffff;
                    font-weight: bold;
                    color: #1d5aa5;                    
                }                
                
                .bk-menu:not(.bk-divider){
                    background-color: #ffffff;
                    color: #1d5aa5;
                }        

                .bk-menu:not(.bk-divider):hover{
                    background-color: #ffffff;
                    color: #1d5aa5;
                }

                .bk-menu:not(.bk-divider):focus{
                    background-color: #ffffff;
                    color: #1d5aa5;
                }
                '''
       
        self.tabcss = '''
                .bk-tab{
                    background-color: #ffffff;
                    color: #000000;}

                .bk-tab.bk-active{
                    background-color: #ffffff;
                    color: #1d5aa5;
                    font-weight: bold;}
                '''

       #Model specific dictionary initialized here
       #End model specific

        #Get list of analyses
        MYLOGGER.debug('root path'+os.path.abspath(os.sep))
        if connectiontype==2:
            try:
                self.rootPath=os.path.abspath(os.sep)  #.replace(os.sep,'/')
                self.modelPath=os.path.join(self.rootPath,"app",self.modelprefix)   #Later, maybe store all loss source files here
                self.userPath=os.path.join(self.modelPath,self.userid)
                self.tempPath=os.path.join(self.userPath,"sessiontemp")

                if os.path.isdir(self.userPath):
                    MYLOGGER.debug('User Path exists')
                    self.analysislist=[x for x in os.listdir(self.userPath) if x!='sessiontemp']
            except:
                self.analysislist=[]
        elif connectiontype==3:
            self.tempPath=f"C:/{self.modelprefix}/sessiontemp"

        MYLOGGER.debug('Starting dictionaries and widgets initialization')
        self.initializeDictionariesAndWidgets()
        MYLOGGER.debug('Resuming after dictionaries and widgets initialized')   

    #Used to handle all events from buttons or set up with watch. RadioButtonGroups, for example.
    def eventresponses(self,*events):
        MYLOGGER.debug('Enter eventresponses')

        if self.updating==True:
            return
        
        self.updating=True
        ## Model-specific code here
        for event in events:
            try:
                tag=event.obj.tag
            except:
                tag=""
            try:
                name=event.obj.name
            except:
                name=""

            pFns.panelEventResponses(self,event,name,tag)                                                                          
        self.updating=False

    def createMainMenuButtons(self):
        result=pn.Column(width=250)
        temp=(self.panelDicts["Main Menu"]
                .with_columns(pl.col("Submenu Items").str.split(",").cast(pl.List(pl.Utf8)).alias("Submenu Items"))
                .with_columns(pl.col("Action Keys").str.split(",").cast(pl.List(pl.Utf8)).alias("Action Keys")))

        for row in temp.rows(named=True):
            if row['Submenu Items'][0]=='None':   #Button, not dropdown
                if row['Icon']=='None':
                    self.mainMenuButtonDict[row['Main Menu Item']]=pn.widgets.Button(name=row['Name'],button_type="light", width_policy='max', height=50,stylesheets=[self.mainmenucss])
                else:
                    if row['Icon'][:6]=='icons.':
                        _icon=eval(row['Icon'])
                    else:
                        _icon=row['Icon']                    
                    self.mainMenuButtonDict[row['Main Menu Item']]=pn.widgets.Button(name=row['Name'], icon=_icon, icon_size='2em',button_type="light", width_policy='max', height=50,stylesheets=[self.mainmenucss])

                self.mainMenuButtonDict[row['Main Menu Item']].on_click(self.executeMainMenuAction)
                result.append(self.mainMenuButtonDict[row['Main Menu Item']])
                if row['End of Section']=='True':
                    result.append(pn.Spacer(height=10))
                    result.append(pn.pane.HTML(styles={'height':'2px','width':'250px','background-color': '#bfbfbf'},margin=(0,0,0,0)))
                    result.append(pn.Spacer(height=10))
            else:
                submenuitems=[x if x!='None' else None for x in row['Submenu Items'] ]
                actionkeys=[x if x!='None' else None for x in row['Action Keys'] ]
                submenutuples=list(zip(submenuitems,actionkeys))
                submenutuples=[x if x!=(None,None) else None for x in submenutuples ]
                if row['Icon']=='None':
                    self.mainMenuButtonDict[row['Main Menu Item']]=pn.widgets.MenuButton(name=row['Name'], items=submenutuples, button_type="light", width_policy='max', height=50,stylesheets=[self.mainmenucss])
                else:
                    if row['Icon'][:6]=='icons.':
                        _icon=eval(row['Icon'])
                    else:
                        _icon=row['Icon']
                    self.mainMenuButtonDict[row['Main Menu Item']]=pn.widgets.MenuButton(name=row['Name'], icon=_icon, icon_size='2em',items=submenutuples, button_type="light", width_policy='max', height=50,stylesheets=[self.mainmenucss])
                self.mainMenuButtonDict[row['Main Menu Item']].on_click(self.executeMainMenuAction)    
                result.append(self.mainMenuButtonDict[row['Main Menu Item']])
                if row['End of Section']=='True':
                    result.append(pn.Spacer(height=10))
                    result.append(pn.pane.HTML(styles={'height':'2px','width':'250px','background-color': '#bfbfbf'},margin=(0,0,0,0)))
                    result.append(pn.Spacer(height=10))
        return result

    def executeMainMenuAction(self,event=None):
        try:
            pn.state.notifications.position = 'top-right'
            if event.name=='clicks':
                action=event.obj.name
            elif event.name=='clicked':
                action=event.obj.clicked
            pn.state.notifications.success(action,duration=2500)
            MYLOGGER.debug('Execute Main Menu Action: '+action)
            self.createMainPanel(action)
        except:
            pass

    def initializeAnalysis(self):  
        #Initialized analysis variable
        MYLOGGER.debug('Enter initializeAnalysis')

        self.analysis= analysis.Analysis(self.connectiontype,self.modeltype,self.modelprefix,None,self.filename,
                                         configfile=self.configfile,panel=self)
        if self.analysis.error=="":
            self.analysisname=self.analysis.preppedspecs['analysisname']

            #Initialize dataform formats
            self.dataformWidgetBlanksDict=dfc.createAllSpecWidgetBlanks(self.analysis.initialspecs,self.panelDicts)
            
            self.specWidgetDict=dfc.createSpecWidgets(self.analysis.initialspecs,  
                                                                    self.dataformWidgetBlanksDict,
                                                                    self.specWidgetDict,
                                                                    self.panelDicts,
                                                                    True,None,None)

            self.dataformWidgetDict=dfc.createDataFormWidgetDict(self.analysis.initialspecs,  
                                                                    self.dataformWidgetBlanksDict,
                                                                    self.specWidgetDict,
                                                                    {},
                                                                    self.panelDicts,
                                                                    True,None,None)

            self.buildDataformDict()                   
            self.buildTabStructure()

            try:       
                mFns.modelSpecificAnalysisSteps(self.analysis,self.connectiontype,self.modelprefix,self)
            except:
                pass

    def cleanPanelSpecTables(self):
        MYLOGGER.debug('Enter cleanPanelSpecTables')
        # Lookup "<Info or Widget Type>|<Data Format>" -> Panel widget type (the table stays under "Map Widget Types")
        self.panelDicts["dict_panelMapWidgetTypes"]=dict(zip(self.panelDicts["Map Widget Types"]['Info or Widget Type']+"|"+self.panelDicts["Map Widget Types"]['Data Format'],
                                                             self.panelDicts["Map Widget Types"]['Panel Widget Type']))
        self.panelDicts["Data Types"]=(dfReplaceNanNone(self.panelDicts["Data Types"])
                                                .with_columns([pl.col('Tab Number').cast(pl.Float64).round(0).cast(pl.Int64).alias('Tab Number'),
                                                               pl.col('Column Order').cast(pl.Float64).round(0).cast(pl.Int64).alias('Column Order'),
                                                               pl.when((pl.col('Checkbox Group').is_not_null()) & (pl.col('Data Type')=='Checkbox Group'))
                                                               .then(pl.lit('String'))
                                                               .otherwise(pl.col('Data Format'))
                                                               .alias('Data Format'),
                                                               pl.when((pl.col('Data Type')=='Single Select'))
                                                               .then(pl.col('Allow Blank in Select').fill_null('False'))
                                                               .otherwise(pl.col('Allow Blank in Select'))
                                                               .alias('Allow Blank in Select')])
                                                .with_columns(pl.when(pl.col('Data Type')=='Boolean')
                                                               .then(pl.lit(None))
                                                               .otherwise(pl.col('Data Format'))
                                                               .alias('Data Format'))
                                                .with_columns(pl.col('Allow Blank in Select').replace_strict({'True':True,'False':False},default=None).alias('Allow Blank in Select'))
                                                .sort(['Spec Sheet','Tab Number','Column Order']))        
        self.panelDicts["Dataform Tabs"]=(dfReplaceNanNone(self.panelDicts["Dataform Tabs"])
                                                   .with_columns(pl.col('Tab Number').cast(pl.Float64).round(0).cast(pl.Int64).alias('Tab Number'))
                                                   .sort(['Spec Sheet','Tab Number']))
        
        self.panelDicts["Dataforms"]=(dfReplaceNanNone(self.panelDicts["Dataforms"])
                                                    .with_columns([pl.col('Dataform Group').cast(pl.Float64).round(0).cast(pl.Int64).alias('Dataform Group'),
                                                                   pl.col('Order').cast(pl.Float64).round(0).cast(pl.Int64).alias('Order')])
                                                    .sort(['Spec Sheet','Dataform Group','Order']))    
        
        self.panelDicts["dict_panelSpecMap"]=dict(zip(self.panelDicts["Dataforms"]['Dataform Name'],self.panelDicts["Dataforms"]['Spec Sheet'])) 
        
        #Clean specs for checkbox group info
        temp1=(self.panelDicts["Data Types"]
        .filter((pl.col('Checkbox Group').is_not_null()) & (pl.col('Data Type')=='Checkbox Group'))
        .with_columns([pl.col('Tab Number').min().over(['Spec Sheet','Checkbox Group']).alias('Tab Number'),
                        pl.col('Column Order').min().over(['Spec Sheet','Checkbox Group']).alias('Column Order')]))
        
        temp1a=temp1.select(['Spec Sheet','Column Name','Checkbox Group'])

        temp2=(self.panelDicts["Data Types"]
        .filter(~((pl.col('Checkbox Group').is_not_null()) & (pl.col('Data Type')=='Checkbox Group'))))

        self.panelDicts["dict_checkboxGroupInfo"]={}
        for spec in temp1.get_column('Spec Sheet').unique().to_list():
            self.panelDicts["dict_checkboxGroupInfo"][spec]={}
            for group in temp1.filter(pl.col('Spec Sheet')==spec).get_column('Checkbox Group').unique().to_list():
                self.panelDicts["dict_checkboxGroupInfo"][spec][group]={}
                collist=temp1.filter((pl.col('Spec Sheet')==spec) & (pl.col('Checkbox Group')==group)).get_column('Column Name').to_list()
                for col in collist:
                    self.panelDicts["dict_checkboxGroupInfo"][spec][group][col]={}

                    try:
                        tempList=temp1.filter((pl.col('Spec Sheet')==spec) & (pl.col('Checkbox Group')==group) & (pl.col('Column Name')==col)).get_column('Visible if Checked')[0].split(',')
                    except:
                        tempList=[]
                    self.panelDicts["dict_checkboxGroupInfo"][spec][group][col]['Visible if Checked']=tempList
                    
                    try:
                        tempList=temp1.filter((pl.col('Spec Sheet')==spec) & (pl.col('Checkbox Group')==group) & (pl.col('Column Name')==col)).get_column('Invisible if Checked')[0].split(',')
                    except:
                        tempList=[]
                    self.panelDicts["dict_checkboxGroupInfo"][spec][group][col]['Invisible if Checked']=tempList

                    try:
                        tempList=temp1.filter((pl.col('Spec Sheet')==spec) & (pl.col('Checkbox Group')==group) & (pl.col('Column Name')==col)).get_column('Visible if Unchecked')[0].split(',')
                    except:
                        tempList=[]
                    self.panelDicts["dict_checkboxGroupInfo"][spec][group][col]['Visible if Unchecked']=tempList
                    
                    try:
                        tempList=temp1.filter((pl.col('Spec Sheet')==spec) & (pl.col('Checkbox Group')==group) & (pl.col('Column Name')==col)).get_column('Invisible if Unchecked')[0].split(',')
                    except:
                        tempList=[]
                    self.panelDicts["dict_checkboxGroupInfo"][spec][group][col]['Invisible if Unchecked']=tempList                    

        self.panelDicts["dict_checkboxCols"]={}
        for spec in temp1.get_column('Spec Sheet').unique().to_list():
            filteredtemp1a=temp1a.filter(pl.col('Spec Sheet')==spec)
            self.panelDicts["dict_checkboxCols"][spec]=dict(zip(filteredtemp1a.get_column('Column Name'),filteredtemp1a.get_column('Checkbox Group')))                 

        MYLOGGER.debug('Created checkbox group info dictionary')

        temp2=temp2.drop(['Visible if Checked','Invisible if Checked','Visible if Unchecked','Invisible if Unchecked','Checkbox Group'])
        temp1=(temp1
                .select(['Spec Sheet','Checkbox Group','Tab Number','Column Order','Data Type','Column Name','Default'])
                .group_by(['Spec Sheet','Checkbox Group','Tab Number','Column Order','Data Type']).agg(pl.col('Column Name'),pl.col('Default'))
                .rename({'Column Name':'Source if Data Type is Select','Checkbox Group':'Column Name'})
                .with_columns([pl.lit('List').alias('Source Type if Data Type is Select'),
                            pl.lit('String').alias('Data Format'),
                            pl.lit('List').alias('Default Source Type'),
                            pl.col('Source if Data Type is Select').list.join(",").alias('Source if Data Type is Select'),
                            pl.col('Default').list.join(",",ignore_nulls=True).alias('Default')]))  

        self.panelDicts["Data Types"]=pl.concat([temp2,temp1],how='diagonal').sort(['Spec Sheet','Tab Number','Column Order'])

        temp1=(self.panelDicts["Data Types"].select(['Spec Sheet','Column Name','Tab Number']).join(self.panelDicts["Dataform Tabs"].select(['Spec Sheet','Tab Number','Dataform Orientation']),on=['Spec Sheet','Tab Number'],how='left'))
        self.panelDicts["dict_panelDataformOrientations"]={}
        for spec in temp1.get_column('Spec Sheet').unique().to_list(): 
            filteredtemp1=temp1.filter(pl.col('Spec Sheet')==spec)
            self.panelDicts["dict_panelDataformOrientations"][spec]=dict(zip(filteredtemp1.get_column('Column Name'),filteredtemp1.get_column('Dataform Orientation')))

        MYLOGGER.debug('Cleaned panel data types')
    
    def getExecStringToFormatWidgets(self,_type,_parameter,_value): 
        MYLOGGER.debug('Enter getExecStringToFormatWidgets')
        
        if _value == 'true':
            valuestr="True"
        elif _value== "false":
            valuestr= "False"
        elif (_type in ['IntInput']) & (_parameter in ['value','step','page_step_multiplier','start']):
            valuestr=f"{int(round(float(_value),0))}"
        elif _parameter in ['height','width','size']:
            valuestr=f"{int(round(float(_value),0))}"
        elif _value.startswith('code_'): 
            valuestr=f"{_value[5:]}"
        elif _type.startswith('Markdown'):
            valuestr=f"'{_type[9:]} {_value}'"
        elif _parameter=="add_filter":
            valuestr=_value.split(';')
        elif _parameter=="options":  #and not code. that was addressed above.
            valuestr=_value.split(';')
        else:   
            valuestr=f"'{_value}'"

        if (_type=='Select') & (_parameter=='size'):
                result=None
        elif _parameter=="disabled":
            result=None
        elif _parameter=='hidden name':
            result= f".tag={valuestr}"
        elif _parameter=='add_filter':
            result=f".add_filter(self.widgetDict['{valuestr[0]}'],'{valuestr[1]}')"
        else:
            result=f".{_parameter}={valuestr}"

        return result
        
    def createWidgets(self):
        def buildWidget(_widgetid,_widgettype,_dataformat="None"):
            try:
                parameters=self.panelDicts["Widget Specs"].filter((pl.col('Widget or Group ID')==_widgetid)&(pl.col('Parameter Value').is_not_null())).unique(subset=['Parameter Type'],keep='first')
            except:
                parameters=pl.DataFrame()

            tempwidgettype=self.panelDicts["dict_panelMapWidgetTypes"][_widgettype+"|"+_dataformat]
            
            if tempwidgettype=="Button":
                result=pn.widgets.Button()
                result.on_click(self.eventresponses) 
            elif tempwidgettype=="TextInput":
                result=pn.widgets.TextInput()
                self.eventwatches.append(result.param.watch(self.eventresponses,['value'],onlychanged=True))
            elif tempwidgettype=="FileInput":
                result=pn.widgets.FileInput()
                self.eventwatches.append(result.param.watch(self.eventresponses,['value','name','tags'],onlychanged=True))
            elif tempwidgettype=="FileDownload":
                result=pn.widgets.FileDownload(callback=pn.bind(self.fileDownloadCallback,_widgetid),filename='placeholder')
            elif tempwidgettype=="LoadingSpinner":
                result=pn.widgets.LoadingSpinner()
            elif tempwidgettype.startswith("Markdown"):
                result=pn.pane.Markdown()
            elif tempwidgettype=="RadioButtonGroup":
                result=pn.widgets.RadioButtonGroup()
                self.eventwatches.append(result.param.watch(self.eventresponses,['value','name','tags'],onlychanged=False))
            elif tempwidgettype=="Select":
                if 'size' in parameters.get_column('Parameter Type').to_list():
                    size=parameters.filter(pl.col('Parameter Type')=='size').get_column('Parameter Value').to_list()[0]
                else:
                    size=None
                if size==None:
                    result=pn.widgets.Select()
                else:
                    result=pn.widgets.Select(size=int(size))
                self.eventwatches.append(result.param.watch(self.eventresponses,['value'],onlychanged=False))
            elif tempwidgettype=="FileSelector":
                result=pn.widgets.FileSelector()
                self.eventwatches.append(result.param.watch(self.eventresponses,['value'],onlychanged=True))
            elif tempwidgettype=="Tabulator":
                result=pn.widgets.Tabulator(name=_widgetid)
                self.eventwatches.append(result.param.watch(self.eventresponses,['value'],onlychanged=True))
            elif tempwidgettype=="IntInput":
                result=pn.widgets.IntInput()
                self.eventwatches.append(result.param.watch(self.eventresponses,['value'],onlychanged=True))

            for row in parameters.rows(named=True):
                parametervalue=row['Parameter Value']
                parametertype=row['Parameter Type']
                execstring=self.getExecStringToFormatWidgets(tempwidgettype,parametertype,parametervalue)
                if execstring!=None:
                    try:
                        exec("result"+execstring)
                    except:
                        MYLOGGER.debug(f"Couldn't assign parameter: {parametertype},{parametervalue},{execstring}")
            return result
        
        self.widgetDict={}  

        for row in self.panelDicts["Widgets"].rows(named=True):
            if row['Widget Type'] in ['None','']:
                pass
            else:
                self.widgetDict[row['Widget ID']]=buildWidget(row['Widget ID'],row['Widget Type']) 

    def createWidgetGroups(self): 
        def addLevel(groupID):  #used to ensure that widget groups are built in the correct order (in case of nested groups)
            if groupID not in self.panelDicts["Widget Group Members"].filter(pl.col('Member Type')=="Widget Group").get_column('Member ID').unique().to_list():
                return 0
            else:
                widgetgroupid=self.panelDicts["Widget Group Members"].filter(pl.col('Member ID')==groupID).get_column('Widget Group ID').to_list()[0]
                return 1+addLevel(widgetgroupid)
        
        self.widgetGroupDict={}
        self.panelDicts["Widget Groups"]=(self.panelDicts["Widget Groups"]
                                                    .filter(pl.col('Container Type').is_in(['Column','Row','Card','WidgetBox','Modal']))
                                                    .with_columns(pl.col('Widget Group ID').map_elements(lambda x: addLevel(x),return_dtype=pl.Int64).alias('Level')))

        if self.panelDicts["Widget Group Members"].shape[0]>0:
            for level in range(max(self.panelDicts["Widget Groups"].get_column('Level').to_list()),-1,-1):
                for row in self.panelDicts["Widget Groups"].filter(pl.col('Level')==level).rows(named=True):
                    if row['Container Type']=='WidgetBox':
                        result= pn.WidgetBox(name=row['Widget Group ID'])
                    elif row['Container Type']=='Card':
                        result=pn.Card(name=row['Widget Group ID'])
                    elif row['Container Type']=='Row':
                            result=pn.Row()
                    elif row['Container Type']=='Column':
                            result=pn.Column()

                    parameters=self.panelDicts["Widget Specs"].filter((pl.col('Widget or Group ID')==row['Widget Group ID'])&(pl.col('Parameter Value').is_not_null())).unique(subset=['Parameter Type'],keep='first')                        
                    for row in parameters.rows(named=True):
                        parametervalue=row['Parameter Value']
                        parametertype=row['Parameter Type']
                        execstring=self.getExecStringToFormatWidgets(row['Container Type'],parametertype,parametervalue)
                        exec("result"+execstring)
                        
                    for row in (self.panelDicts["Widget Group Members"]
                                .filter(pl.col('Widget Group ID')==row['Widget Group ID'])
                                .sort('Order',descending=False)
                                .rows(named=True)):
                            if row['Member Type']=='Widget':
                                result.append(self.widgetDict[row['Member ID']])
                            elif row['Member Type']=='Widget Group':
                                result.append(self.widgetGroupDict[row['Member ID']])
                        
                    self.widgetGroupDict[row['Widget Group ID']]=result

    def buildTabStructure(self):
        MYLOGGER.debug("Beginning buildTabStructure")
        
        def addLevel(tabID):
            _parent=tabsTable.filter(pl.col('Tab ID')==tabID).get_column('Parent')[0]
            _parenttype=tabsTable.filter(pl.col('Tab ID')==tabID).get_column('Parent Type')[0]
            if _parenttype!="Tab":
                return 0
            else:
                return 1+addLevel(_parent)  #.lower()

        #Add level to tabs table to determine how order for building tabs. Subtabs must be built before parent tabs.
        try:
            tabsTable=self.panelDicts["Tabs"].filter(pl.col('Tab ID')!='None')
            MYLOGGER.debug(tabsTable.shape[0])
        except:
            tabsTable=pl.DataFrame()
            MYLOGGER.debug('No panelTabs')

        if not self.analysis:
            if tabsTable.shape[0]>0:
                tabsTable=tabsTable.unique(subset=['Tab ID'],keep='first')
                tabsTable=(tabsTable
                        .with_columns((pl.col('Tab ID').map_elements(lambda x: addLevel(x),return_dtype=pl.Int64).alias('Level')))
                        .with_columns(pl.when(pl.col('Tab ID').is_in(tabsTable.get_column('Parent').to_list()))
                                        .then(pl.lit('Tabs'))
                                        .otherwise(pl.col('Content Type'))
                                        .alias('Content Type')))
                tablist=tabsTable.filter((pl.col('Content Type')=='Tabs')).sort('Level',descending=True).get_column('Tab ID').to_list()
                tablist2=tabsTable.filter(pl.col('Parent').is_in(tablist).not_()).get_column('Parent').unique().to_list()
                tablist=tablist+tablist2
                MYLOGGER.debug('Tablist: '+str(tablist))
                for parent in tablist:
                    temptbl=tabsTable.filter(pl.col('Parent')==parent).sort('Order',descending=False)
                
                    self.tabsDict[parent]=pn.Tabs(dynamic=True,stylesheets=[self.tabcss])
                    for row in temptbl.rows(named=True):
                        if row['Content Type']=='Tabs':
                            self.tabsDict[parent].append((row['Tab Name'],self.tabsDict[row['Tab ID']]))
                        elif row['Content Type']=='Dataform':
                            self.tabsDict[parent].append((row['Tab Name'],pn.Column()))
                        elif row['Content Type']=='Widget Group':
                            self.tabsDict[parent].append((row['Tab Name'],self.widgetGroupDict[row['Parameter']]))
                        else:
                            self.tabsDict[parent].append((row['Tab Name'],pn.pane.Markdown("### "+str(row['Tab ID']))))
                self.eventwatches.append(self.tabsDict[parent].param.watch(self.eventresponses,'active',onlychanged=True))
        else:   #After analysis is initialized, add tabs for dataforms
            if tabsTable.shape[0]>0:
                tabsTable=tabsTable.unique(subset=['Tab ID'],keep='first')
                tabsTable=(tabsTable
                        .with_columns((pl.col('Tab ID').map_elements(lambda x: addLevel(x)).alias('Level')))
                        .with_columns(pl.when(pl.col('Tab ID').is_in(tabsTable.get_column('Parent').to_list()))
                                        .then(pl.lit('Tabs'))
                                        .otherwise(pl.col('Content Type'))
                                        .alias('Content Type')))
                tablist=tabsTable.filter((pl.col('Content Type')=='Tabs')).sort('Level',descending=True).get_column('Tab ID').to_list()
                tablist2=tabsTable.filter(pl.col('Parent').is_in(tablist).not_()).get_column('Parent').unique().to_list()
                tablist=tablist+tablist2
                MYLOGGER.debug('Tablist: '+str(tablist))
                for parent in tablist:
                    temptbl=tabsTable.filter(pl.col('Parent')==parent).sort('Order',descending=False)
                    countindex=0

                    for row in temptbl.rows(named=True):

                        if row['Content Type']=='Dataform':
                            self.tabsDict[parent][countindex]=self.dataformDict[int(float(row['Parameter']))].view
                        countindex+=1
        MYLOGGER.debug('Exiting buildTabStructure')

    def buildDataformDict(self):
        MYLOGGER.debug('Entered buildDataformDict')

        for dfgrp in self.panelDicts["Dataforms"]['Dataform Group'].unique().to_list():
            MYLOGGER.debug(f'Calling DataForm for {dfgrp}')
            self.dataformDict[int(float(dfgrp))]=dfc.Dataform(parent=self,group=int(float(dfgrp))) 
            MYLOGGER.debug('Dataform created')

    def enablemenubuttons(self):   #TODO
        for key in self.panelDicts["dict_panelMainMenuIcons"].keys():
            self.mainMenuButtonDict[key].disabled=False       
    
    def createMainPanel(self,selected):
        contenttype="None"
        MYLOGGER.debug(f'Create Main Panel for {selected}')
        contenttype=self.panelDicts["Main Menu Actions"].filter(pl.col('Action Key')==selected).get_column('Content Type').to_list()[0]
        if contenttype=='Tabs':
            contentparameter=selected
        else:
            contentparameter=self.panelDicts["Main Menu Actions"].filter(pl.col('Action Key')==selected).get_column('Parameter').to_list()[0]

        if selected=="Data Exploration":
            self.mainAreaWidget[0]=self.analysis.kpichart.view      #TODO
        elif contenttype=='Widget Group':
            self.mainAreaWidget[0]=self.widgetGroupDict[contentparameter]
        elif contenttype=='Results':
            #Chart any spec tables that are dataframes. CreateChart works in pandas, so polars frames are converted.
            datadict={}
            for key,val in self.analysis.preppedspecs.items():
                if isinstance(val,pl.DataFrame):
                    datadict[key]=val.to_pandas()
                elif type(val).__name__=='DataFrame':
                    datadict[key]=val
            try:
                self.mainAreaWidget[0]=cc.CreateChart(parent=self,datasourcetype='Dictionary',
                                                      dictionary={'dataDictionary':datadict,'enableDesign':True}).view
            except Exception:
                MYLOGGER.exception('Could not create Results chart')
        elif contenttype=='Tabs':
            MYLOGGER.debug('Selected: '+selected+', Content Type: '+contenttype+', Content Parameter: '+contentparameter)
            self.mainAreaWidget[0]=self.tabsDict[contentparameter]
        elif contenttype=='Dataform':
            MYLOGGER.debug('Selected: '+selected+', Content Type: '+contenttype+', Content Parameter: '+contentparameter)
            self.mainAreaWidget[0]=self.dataformDict[int(float(contentparameter))].view 
        elif contenttype=='On-Action Function':
            fnname=contentparameter.split('|')[0]
            try:
                fntags=contentparameter.split('|')[1].split(',')
            except:
                fntags=[]

            try:
                self.mainAreaWidget[0]=pFns.panelEventResponses(self,None,fnname,fntags)
            except:
                pass
        else:  #if has submenu, pick first item on list and show that
            self.mainAreaWidget[0]=pn.pane.Markdown("### "+str(selected)) 

    def refreshAnalysis(self,type='All'):
        try:
            shutil.rmtree(self.analysis.preppedspecs['dataparquetpath'])
        except:
            pass
        createFolderIfNot(self.analysis.preppedspecs['dataparquetpath'][:-13],"DataParquets")
        self.analysis.getResults('All','Gross Ceded Net Statistics')
        self.analysis.getResults('All','Premium Allocations')
        self.analysis.getResults('All','Theoretical Premiums')
        
    def additionalAnalysisInitializationSteps(self):
        if not isinstance(self.analysis,str):
            MYLOGGER.debug('Analysis is instantiated')
            MYLOGGER.debug('Exiting additionalAnalysisInitializationSteps')
        else:
            MYLOGGER.debug('Analysis is not instantiated')
            self.analysis=None

    def fileDownloadCallback(self,widgetID):
        #All fileDownload widgets will use this callback
        return pFns.download_file_callback(self,widgetID)

    def initializeDictionariesAndWidgets(self):
        #Import all Panel dictionaries, change keys to names (so not all lowercase) and create tables for certain dictionaries
        MYLOGGER.debug('Enter initializeDictionariesAndWidgets')

        for key in self.panelDictList:
            try:
                data={k:v for k,v in self.configdict[key].items() if k!='panel'}
                self.panelDicts[key]=convertDictToTable(self.configdict,key,data)
            except Exception:
                MYLOGGER.exception(f'Could not build Panel table {key}')
                self.panelDicts[key]=pl.DataFrame()

            #If source table was empty, first column will have first row = 'None'. Delete that row, so table is actually empty.
            if self.panelDicts[key].width:
                self.panelDicts[key]=self.panelDicts[key].filter(pl.col(self.panelDicts[key].columns[0])!='None')
            MYLOGGER.debug('Dictionaries and tables initialized '+key)                

        ############MODEL SPECIFIC CODE ################
        if self.connectiontype==2:
            self.panelDicts["Main Menu Actions"]=(self.panelDicts["Main Menu Actions"]
                                                          .filter(pl.col('Action Key')!='Select Analysis_Local')
                                                          .with_columns(pl.col('Action Key').replace('Select Analysis_Server','Select Analysis')))
        elif self.connectiontype==3:
            self.panelDicts["Main Menu Actions"]=(self.panelDicts["Main Menu Actions"]
                                                          .filter(pl.col('Action Key')!='Select Analysis_Server')
                                                          .with_columns(pl.col('Action Key').replace('Select Analysis_Local','Select Analysis')))
        ############END MODEL SPECIFIC CODE ############

        self.cleanPanelSpecTables()   
        self.createWidgets()
        self.createWidgetGroups()
        self.buildTabStructure()

    @pn.depends('analysisname', watch=True)
    def update_analysisname(self):
        try:
            self.analysisnamewidget.object = f"Current Analysis: {self.analysisname}"
        except:
            pass

    def view(self):
        #Add BMS logo to dashboard
        pic_pathway = panel_resource_path("BMS-Logo-modified.png")
        with open(pic_pathway, "rb") as img_file:
            bms_logo = base64.b64encode(img_file.read()).decode('utf-8')
        img_markdown = f'<img src="data:image/jpg;base64,{bms_logo}" align="right" width="100"/>'

        pn.extension(notifications=True)
        pn.state.notifications.position='top-right'
        pn.extension('tabulator') 
        pn.extension('terminal',console_output='disable')
        pn.extension('modal')

        #Create main menu buttons
        self.mainmenuwidgets=self.createMainMenuButtons()
        self.analysisnamewidget=pn.pane.Markdown("",styles={'color':'#1d5aa5'})

        self.template = pn.template.BootstrapTemplate(
            title=self.modeltype,  
            header_background='#ffffff',
            sidebar=[self.mainmenuwidgets,self.analysisnamewidget,],  
            sidebar_width=250,
            busy_indicator=pn.indicators.BooleanStatus(value=True),
            favicon="./BMS-Logo-modified.png",
            raw_css=[RAW_CSS]
        )

        self.template.header.append(pn.HSpacer(width=50))
        self.template.header.append(pn.pane.Markdown(img_markdown, align="end"))
        self.template.modal.append(pn.Column())
        self.mainAreaWidget=pn.Column(pn.pane.Markdown("### Welcome to the "+str(self.modeltype)))
        
        self.template.main.append(self.mainAreaWidget)
        
        return self.template 